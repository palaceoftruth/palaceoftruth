const COLORS: Record<string, string> = {
  related_to: "bg-blue-900/50 text-blue-300",
  expands_on: "bg-green-900/50 text-green-300",
  contradicts: "bg-red-900/50 text-red-300",
  prerequisite_of: "bg-yellow-900/50 text-yellow-300",
  example_of: "bg-purple-900/50 text-purple-300",
};

interface RelationshipBadgeProps {
  relationship: string;
}

export default function RelationshipBadge({ relationship }: RelationshipBadgeProps) {
  const cls = COLORS[relationship] ?? "bg-gray-800 text-gray-400";
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {relationship.replace(/_/g, " ")}
    </span>
  );
}
