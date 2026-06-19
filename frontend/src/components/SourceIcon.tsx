import { Youtube, Globe, FileText, StickyNote, Image, HelpCircle } from "lucide-react";
import type { LucideIcon } from "lucide-react";

const ICONS: Record<string, LucideIcon> = {
  youtube: Youtube,
  media: Youtube,
  webpage: Globe,
  pdf: FileText,
  doc: FileText,
  image: Image,
  note: StickyNote,
};

const COLORS: Record<string, string> = {
  youtube: "text-red-400",
  media: "text-red-400",
  webpage: "text-blue-400",
  pdf: "text-orange-400",
  doc: "text-orange-400",
  image: "text-pink-400",
  note: "text-purple-400",
};

interface SourceIconProps {
  sourceType: string;
  className?: string;
}

export default function SourceIcon({ sourceType, className = "w-4 h-4" }: SourceIconProps) {
  const Icon = ICONS[sourceType] ?? HelpCircle;
  const color = COLORS[sourceType] ?? "text-gray-400";
  return <Icon className={`${className} ${color}`} />;
}

export { COLORS as SOURCE_COLORS };
