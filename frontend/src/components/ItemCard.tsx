import { useNavigate } from "react-router-dom";
import type { Item } from "../api/types";
import SourceIcon from "./SourceIcon";

interface ItemCardProps {
  item: Item;
  onTagClick?: (tag: string) => void;
}

function metadataString(item: Item, key: string): string | null {
  const value = item.metadata_?.[key];
  return typeof value === "string" ? value : null;
}

export default function ItemCard({ item, onTagClick }: ItemCardProps) {
  const navigate = useNavigate();
  const feedName = metadataString(item, "feed_name");

  return (
    <div
      className="sb-list-card cursor-pointer p-4"
      onClick={() => navigate(`/items/${item.id}`)}
    >
      <div className="flex items-start gap-3">
        <SourceIcon sourceType={item.source_type} className="w-4 h-4 mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <h3 className="truncate text-sm font-medium text-zinc-100">{item.title}</h3>
          <p className="mt-0.5 text-xs text-zinc-500">
            {new Date(item.created_at).toLocaleDateString()}
            {item.source_type === "feed_article" && feedName && (
              <span className="ml-2 text-amber-300/80">via {feedName}</span>
            )}
          </p>
          {item.summary && (
            <p className="mt-2 line-clamp-2 text-sm leading-6 text-zinc-400">{item.summary}</p>
          )}
          {item.tags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {item.tags.slice(0, 5).map((tag) => (
                <button
                  key={tag}
                  onClick={(e) => {
                    e.stopPropagation();
                    onTagClick?.(tag);
                  }}
                  className="sb-chip sb-chip-inactive cursor-pointer px-2.5 py-1"
                >
                  {tag}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
