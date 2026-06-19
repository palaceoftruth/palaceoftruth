import { useRef, useState } from "react";
import { FileText, Globe, Image, MessageSquareText, StickyNote, Tv, Upload } from "lucide-react";

import { api, ApiError } from "../api/client";
import JobStatusCard from "../components/JobStatusCard";
import PageHeader from "../components/PageHeader";
import { useToast } from "../context/ToastContext";

type Tab = "youtube" | "webpage" | "social" | "doc" | "image" | "note";

const TABS: { id: Tab; label: string; icon: typeof Upload }[] = [
  { id: "youtube", label: "Media / Audio", icon: Tv },
  { id: "webpage", label: "Webpage", icon: Globe },
  { id: "social", label: "Social post", icon: MessageSquareText },
  { id: "doc", label: "Document", icon: FileText },
  { id: "image", label: "Image", icon: Image },
  { id: "note", label: "Note", icon: StickyNote },
];

const DOC_ACCEPT = ".pdf,.docx,.xlsx,.md,.txt";
const IMAGE_ACCEPT = ".jpg,.jpeg,.png,.gif,.webp";

export default function Ingest() {
  const [tab, setTab] = useState<Tab>("youtube");
  const [url, setUrl] = useState("");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [noteTags, setNoteTags] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const handleSubmit = async () => {
    setJobId(null);
    setSubmitting(true);
    try {
      let result;
      if (tab === "youtube") result = await api.ingestMedia(url.trim());
      else if (tab === "webpage" || tab === "social") result = await api.ingestWebpage(url.trim());
      else if (tab === "doc" && file) result = await api.ingestDoc(file);
      else if (tab === "image" && file) result = await api.ingestImage(file);
      else if (tab === "note") {
        const tags = noteTags ? noteTags.split(",").map((t) => t.trim()).filter(Boolean) : [];
        result = await api.ingestNote({ title: noteTitle, content: noteContent, tags });
      }
      if (result) {
        setJobId(result.job_id);
        toast.success("Job queued — processing started");
      }
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const canSubmit = (() => {
    if (submitting) return false;
    if (tab === "youtube" || tab === "webpage" || tab === "social") return url.trim().length > 0;
    if (tab === "doc" || tab === "image") return file !== null;
    if (tab === "note") return noteTitle.trim().length > 0 && noteContent.trim().length > 0;
    return false;
  })();

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Capture"
        title="Add new memory to the workspace"
        description="Queue notes, files, pages, media, and images into the same library pipeline that powers retrieval and Palace organization."
      />

      <section className="sb-panel sb-panel-padding space-y-5">
        <div>
          <p className="sb-section-title">Capture type</p>
          <div className="mt-4 flex flex-wrap gap-2">
            {TABS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                onClick={() => {
                  setTab(id);
                  setJobId(null);
                }}
                className={`sb-chip min-h-[44px] cursor-pointer ${tab === id ? "sb-chip-active" : "sb-chip-inactive"}`}
              >
                <Icon className="h-4 w-4" />
                <span>{label}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          {(tab === "youtube" || tab === "webpage" || tab === "social") ? (
            <div>
              <label className="mb-1 block text-sm text-zinc-400">
                {tab === "youtube" ? "Media / Audio URL" : tab === "social" ? "X / Twitter post URL" : "Webpage URL"}
              </label>
              <input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={
                  tab === "youtube"
                    ? "Paste any audio or video URL — YouTube, podcasts, Vimeo, and more"
                    : tab === "social"
                      ? "https://x.com/username/status/1234567890"
                    : "https://example.com/article"
                }
                className="sb-input"
              />
              {tab === "social" ? (
                <p className="mt-2 text-xs leading-relaxed text-zinc-500">
                  Captures public X/Twitter status URLs through provider metadata instead of a browser session.
                </p>
              ) : null}
            </div>
          ) : null}

          {(tab === "doc" || tab === "image") ? (
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                setFile(e.dataTransfer.files[0] ?? null);
              }}
              onClick={() => fileInputRef.current?.click()}
              className={`cursor-pointer rounded-[28px] border-2 border-dashed p-8 text-center transition duration-200 ease-out ${
                dragOver ? "border-sky-500 bg-sky-950/20" : "border-zinc-700 hover:border-zinc-500"
              }`}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept={tab === "doc" ? DOC_ACCEPT : IMAGE_ACCEPT}
                className="hidden"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              {tab === "doc" ? (
                <FileText className="mx-auto mb-2 h-8 w-8 text-zinc-500" />
              ) : (
                <Image className="mx-auto mb-2 h-8 w-8 text-zinc-500" />
              )}
              {file ? (
                <p className="text-sm font-medium text-zinc-200">{file.name}</p>
              ) : tab === "doc" ? (
                <>
                  <p className="text-sm text-zinc-400">Drop a document here or click to browse</p>
                  <p className="mt-1 text-xs text-zinc-500">PDF, DOCX, XLSX, MD, TXT</p>
                </>
              ) : (
                <>
                  <p className="text-sm text-zinc-400">Drop an image here or click to browse</p>
                  <p className="mt-1 text-xs text-zinc-500">JPG, PNG, GIF, WEBP — analyzed by AI vision</p>
                </>
              )}
            </div>
          ) : null}

          {tab === "note" ? (
            <>
              <div>
                <label className="mb-1 block text-sm text-zinc-400">Title</label>
                <input
                  type="text"
                  value={noteTitle}
                  onChange={(e) => setNoteTitle(e.target.value)}
                  placeholder="Note title"
                  className="sb-input"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-zinc-400">Content</label>
                <textarea
                  value={noteContent}
                  onChange={(e) => setNoteContent(e.target.value)}
                  placeholder="Write your note here…"
                  rows={6}
                  className="sb-textarea resize-none"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-zinc-400">Tags (comma-separated)</label>
                <input
                  type="text"
                  value={noteTags}
                  onChange={(e) => setNoteTags(e.target.value)}
                  placeholder="machine-learning, productivity"
                  className="sb-input"
                />
              </div>
            </>
          ) : null}

          <button onClick={handleSubmit} disabled={!canSubmit} className="sb-button-primary w-full">
            <Upload className="h-4 w-4" />
            {submitting ? "Submitting…" : "Capture"}
          </button>
        </div>
      </section>

      {jobId ? <JobStatusCard jobId={jobId} /> : null}
    </div>
  );
}
