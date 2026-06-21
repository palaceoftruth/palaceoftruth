import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Check, ChevronRight, Compass, GitMerge, Loader2, Map, Milestone, Pencil, RefreshCw, Route, Search, SlidersHorizontal, X } from "lucide-react";

import { api, ApiError } from "../api/client";
import type {
  MemoryScopeType,
  PalaceConsolidationCandidate,
  PalaceControlTower,
  PalaceOverview,
  PalaceRetrieveResponse,
  PalaceRoomDetail,
  PalaceRoomSummary,
  PalaceSyncSource,
  PalaceWingSummary,
} from "../api/types";
import PalaceFreshnessPill from "../components/PalaceFreshnessPill";
import PageHeader from "../components/PageHeader";
import PalaceStateBanner from "../components/PalaceStateBanner";
import ProvenanceDrawer from "../components/ProvenanceDrawer";
import StatePanel from "../components/StatePanel";
import SourceIcon from "../components/SourceIcon";
import { useToast } from "../context/ToastContext";

const SCOPE_OPTIONS: Array<{ value: MemoryScopeType; label: string }> = [
  { value: "session", label: "session" },
  { value: "agent", label: "agent" },
  { value: "workspace", label: "workspace" },
  { value: "tenant_shared", label: "tenant_shared" },
];

const SCOPE_PLACEHOLDER: Record<Exclude<MemoryScopeType, "tenant_shared">, string> = {
  session: "session key",
  agent: "agent key",
  workspace: "workspace key",
};

type MembershipFilter = "all" | "pinned" | "auto";
type RoomSort = "palace" | "name" | "drawers" | "freshness";

const MEMBERSHIP_FILTERS: Array<{ value: MembershipFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "pinned", label: "Pinned" },
  { value: "auto", label: "Auto" },
];

const ROOM_SORT_OPTIONS: Array<{ value: RoomSort; label: string }> = [
  { value: "palace", label: "Palace order" },
  { value: "name", label: "A-Z" },
  { value: "drawers", label: "Most drawers" },
  { value: "freshness", label: "Needs attention" },
];

const FRESHNESS_PRIORITY: Record<PalaceRoomSummary["snapshot_status"]["status"], number> = {
  stale: 0,
  indexing: 1,
  redirected: 2,
  fresh: 3,
};

function membershipLabel(count: number, descriptor?: string): string {
  return `${count} ${descriptor ? `${descriptor} ` : ""}${count === 1 ? "membership" : "memberships"}`;
}

function selectedRoomForWing(wing: PalaceWingSummary | undefined, roomId: string | null): PalaceRoomSummary | null {
  if (!wing || wing.rooms.length === 0) return null;
  if (!roomId) return wing.rooms[0];
  return wing.rooms.find((room) => room.id === roomId) ?? wing.rooms[0];
}

function findWingForRoom(overview: PalaceOverview | null, roomId: string | null): PalaceWingSummary | null {
  if (!overview || !roomId) return null;
  return overview.wings.find((wing) => wing.rooms.some((room) => room.id === roomId)) ?? null;
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function formatCandidateScore(score: number): string {
  return `${Math.round(score * 100)}%`;
}

function formatTraceRoute(route: string): string {
  return route.replace(/_/g, " ");
}

function formatScore(score: number | null | undefined): string | null {
  if (typeof score !== "number") return null;
  return score.toFixed(3);
}

function candidateCounterpart(candidate: PalaceConsolidationCandidate, roomId: string) {
  if (candidate.room_id === roomId) {
    return {
      id: candidate.candidate_room_id,
      name: candidate.candidate_room_name,
      stableKey: candidate.candidate_stable_key,
    };
  }
  return {
    id: candidate.room_id,
    name: candidate.room_name,
    stableKey: candidate.room_stable_key,
  };
}

function roomMatchesFinder(room: PalaceRoomSummary, query: string): boolean {
  if (!query) return true;
  const searchable = [
    room.name,
    room.summary,
    room.stable_key,
    room.snapshot_status.status,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return searchable.includes(query);
}

function sortRoomsForFinder(
  rooms: PalaceRoomSummary[],
  sort: RoomSort,
): PalaceRoomSummary[] {
  return rooms
    .map((room, palaceIndex) => ({ room, palaceIndex }))
    .sort((left, right) => {
      if (sort === "name") {
        return left.room.name.localeCompare(right.room.name);
      }
      if (sort === "drawers") {
        return right.room.item_count - left.room.item_count
          || left.room.name.localeCompare(right.room.name);
      }
      if (sort === "freshness") {
        return FRESHNESS_PRIORITY[left.room.snapshot_status.status] - FRESHNESS_PRIORITY[right.room.snapshot_status.status]
          || right.room.item_count - left.room.item_count
          || left.room.name.localeCompare(right.room.name);
      }
      return left.palaceIndex - right.palaceIndex;
    })
    .map(({ room }) => room);
}

export default function Palace() {
  const [overview, setOverview] = useState<PalaceOverview | null>(null);
  const [controlTower, setControlTower] = useState<PalaceControlTower | null>(null);
  const [syncSources, setSyncSources] = useState<PalaceSyncSource[]>([]);
  const [selectedWingId, setSelectedWingId] = useState<string | null>(null);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(null);
  const [roomDetail, setRoomDetail] = useState<PalaceRoomDetail | null>(null);
  const [trace, setTrace] = useState<PalaceRetrieveResponse | null>(null);
  const [query, setQuery] = useState("");
  const [scopeType, setScopeType] = useState<MemoryScopeType>("tenant_shared");
  const [scopeKey, setScopeKey] = useState("");
  const [loadingOverview, setLoadingOverview] = useState(true);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [loadingRoom, setLoadingRoom] = useState(false);
  const [roomError, setRoomError] = useState<string | null>(null);
  const [editingRoomName, setEditingRoomName] = useState(false);
  const [roomNameDraft, setRoomNameDraft] = useState("");
  const [roomFinderQuery, setRoomFinderQuery] = useState("");
  const [roomSort, setRoomSort] = useState<RoomSort>("palace");
  const [membershipFilter, setMembershipFilter] = useState<MembershipFilter>("all");
  const [membershipSearch, setMembershipSearch] = useState("");
  const [selectedMembershipIds, setSelectedMembershipIds] = useState<string[]>([]);
  const [membershipAction, setMembershipAction] = useState<{ kind: "pin" | "unpin"; itemId?: string } | null>(null);
  const [renamingRoom, setRenamingRoom] = useState(false);
  const [submittingQuery, setSubmittingQuery] = useState(false);
  const navigate = useNavigate();
  const toast = useToast();
  const selectionRef = useRef<{ wingId: string | null; roomId: string | null }>({
    wingId: null,
    roomId: null,
  });

  useEffect(() => {
    selectionRef.current = {
      wingId: selectedWingId,
      roomId: selectedRoomId,
    };
  }, [selectedWingId, selectedRoomId]);

  const loadOverview = async () => {
    setLoadingOverview(true);
    setOverviewError(null);
    try {
      const [nextOverview, sources] = await Promise.all([
        api.getPalaceOverview(),
        api.listPalaceSyncSources(),
      ]);
      const currentRoomWing = findWingForRoom(nextOverview, selectionRef.current.roomId);
      const nextWing =
        currentRoomWing
        ?? nextOverview.wings.find((wing) => wing.id === selectionRef.current.wingId)
        ?? nextOverview.wings[0]
        ?? null;
      const nextRoom = currentRoomWing
        ? selectedRoomForWing(currentRoomWing, selectionRef.current.roomId)
        : selectedRoomForWing(nextWing ?? undefined, selectionRef.current.roomId);

      setOverview(nextOverview);
      setSyncSources(sources);
      setSelectedWingId(nextWing?.id ?? null);
      setSelectedRoomId(nextRoom?.id ?? null);
      void api.getPalaceControlTower().then(setControlTower).catch(() => setControlTower(null));
    } catch (err) {
      setOverviewError(errorMessage(err));
    } finally {
      setLoadingOverview(false);
    }
  };

  useEffect(() => {
    void loadOverview();
    const id = setInterval(() => {
      void loadOverview();
    }, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedWing = useMemo(() => {
    if (!overview) return null;
    return findWingForRoom(overview, selectedRoomId)
      ?? overview.wings.find((wing) => wing.id === selectedWingId)
      ?? overview.wings[0]
      ?? null;
  }, [overview, selectedRoomId, selectedWingId]);

  useEffect(() => {
    setRoomFinderQuery("");
    setRoomSort("palace");
  }, [selectedWing?.id]);

  const visibleRoomsForSelectedWing = useMemo(() => {
    if (!selectedWing) return [];
    const finder = roomFinderQuery.trim().toLowerCase();
    return sortRoomsForFinder(
      selectedWing.rooms.filter((room) => roomMatchesFinder(room, finder)),
      roomSort,
    );
  }, [roomFinderQuery, roomSort, selectedWing]);

  const selectedRoomHiddenByFinder = Boolean(
    selectedRoomId
    && selectedWing
    && selectedWing.rooms.some((room) => room.id === selectedRoomId)
    && !visibleRoomsForSelectedWing.some((room) => room.id === selectedRoomId),
  );

  const selectRoom = (roomId: string) => {
    const wing = findWingForRoom(overview, roomId);
    if (wing) {
      setSelectedWingId(wing.id);
    }
    setSelectedRoomId(roomId);
  };

  useEffect(() => {
    const room = selectedRoomForWing(selectedWing ?? undefined, selectedRoomId);
    if (room && room.id !== selectedRoomId) {
      setSelectedRoomId(room.id);
    }
    if (!room) {
      setSelectedRoomId(null);
      setRoomDetail(null);
      setRoomError(null);
    }
  }, [selectedWing, selectedRoomId]);

  useEffect(() => {
    if (!selectedRoomId) return;
    let active = true;
    setLoadingRoom(true);
    setRoomError(null);
    setRoomDetail(null);
    void api
      .getPalaceRoom(selectedRoomId)
      .then((detail) => {
        if (!active) return;
        setRoomDetail(detail);
      })
      .catch((err) => {
        if (!active) return;
        setRoomError(errorMessage(err));
      })
      .finally(() => {
        if (active) setLoadingRoom(false);
      });
    return () => {
      active = false;
    };
  }, [selectedRoomId]);

  useEffect(() => {
    setEditingRoomName(false);
    setRoomNameDraft(roomDetail?.room.name ?? "");
    setMembershipFilter("all");
    setMembershipSearch("");
    setSelectedMembershipIds([]);
  }, [roomDetail?.room.id, roomDetail?.room.name]);

  useEffect(() => {
    const membershipIds = new Set((roomDetail?.memberships ?? []).map((membership) => membership.item_id));
    setSelectedMembershipIds((current) => current.filter((itemId) => membershipIds.has(itemId)));
  }, [roomDetail?.memberships]);

  const roomEditorStats = useMemo(() => {
    const memberships = roomDetail?.memberships ?? [];
    const pinned = memberships.filter((membership) => membership.pinned).length;
    const auto = memberships.length - pinned;
    return {
      total: memberships.length,
      pinned,
      auto,
    };
  }, [roomDetail?.memberships]);

  const filteredMemberships = useMemo(() => {
    const memberships = roomDetail?.memberships ?? [];
    const search = membershipSearch.trim().toLowerCase();
    return memberships.filter((membership) => {
      if (membershipFilter === "pinned" && !membership.pinned) return false;
      if (membershipFilter === "auto" && membership.pinned) return false;
      if (!search) return true;
      return [membership.title, membership.summary, membership.source_type, membership.membership_kind]
        .filter(Boolean)
        .some((value) => value!.toLowerCase().includes(search));
    });
  }, [membershipFilter, membershipSearch, roomDetail?.memberships]);

  const selectedMembershipIdSet = useMemo(() => new Set(selectedMembershipIds), [selectedMembershipIds]);

  const selectedMemberships = useMemo(() => {
    const memberships = roomDetail?.memberships ?? [];
    return memberships.filter((membership) => selectedMembershipIdSet.has(membership.item_id));
  }, [roomDetail?.memberships, selectedMembershipIdSet]);

  const selectedPinnedCount = useMemo(
    () => selectedMemberships.filter((membership) => membership.pinned).length,
    [selectedMemberships],
  );
  const selectedAutoCount = selectedMemberships.length - selectedPinnedCount;
  const selectedVisibleCount = filteredMemberships.filter((membership) => selectedMembershipIdSet.has(membership.item_id)).length;
  const allVisibleMembershipsSelected = filteredMemberships.length > 0 && selectedVisibleCount === filteredMemberships.length;
  const membershipActionInFlight = membershipAction !== null;

  const roomConsolidationCandidates = useMemo(() => {
    if (!selectedRoomId) return [];
    return (controlTower?.consolidation.candidates ?? []).filter(
      (candidate) => candidate.room_id === selectedRoomId || candidate.candidate_room_id === selectedRoomId,
    );
  }, [controlTower?.consolidation.candidates, selectedRoomId]);

  const handleRunPalace = async () => {
    try {
      await api.startPalaceRun();
      await loadOverview();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    }
  };

  const handleSubmitQuery = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!query.trim()) return;
    const normalizedScopeKey = scopeType === "tenant_shared" ? undefined : scopeKey.trim();
    if (scopeType !== "tenant_shared" && !normalizedScopeKey) {
      toast.error(`Enter a ${scopeType} scope key before tracing retrieval.`);
      return;
    }
    setSubmittingQuery(true);
    try {
      const result = await api.retrievePalace({
        query: query.trim(),
        ...(selectedRoomId ? { room_id: selectedRoomId } : {}),
        limit: 5,
        scope_type: scopeType,
        ...(normalizedScopeKey ? { scope_key: normalizedScopeKey } : {}),
      });
      setTrace(result);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmittingQuery(false);
    }
  };

  const retryRoom = async () => {
    if (!selectedRoomId) return;
    setLoadingRoom(true);
    setRoomError(null);
    try {
      setRoomDetail(await api.getPalaceRoom(selectedRoomId));
    } catch (err) {
      setRoomDetail(null);
      setRoomError(errorMessage(err));
    } finally {
      setLoadingRoom(false);
    }
  };

  const refreshRoom = async () => {
    if (selectedRoomId) {
      await retryRoom();
    }
    await loadOverview();
  };

  const handleRenameRoom = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!selectedRoomId || !roomDetail) return;
    const nextName = roomNameDraft.trim();
    if (!nextName) {
      toast.error("Enter a room name before saving.");
      return;
    }
    if (nextName === roomDetail.room.name) {
      setEditingRoomName(false);
      return;
    }
    setRenamingRoom(true);
    try {
      const detail = await api.updatePalaceRoom(selectedRoomId, { name: nextName });
      setRoomDetail(detail);
      setEditingRoomName(false);
      await loadOverview();
      toast.success("Room name updated.");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setRenamingRoom(false);
    }
  };

  const toggleMembershipSelection = (itemId: string, checked: boolean) => {
    setSelectedMembershipIds((current) => {
      if (checked) {
        return current.includes(itemId) ? current : [...current, itemId];
      }
      return current.filter((selectedItemId) => selectedItemId !== itemId);
    });
  };

  const selectVisibleMemberships = () => {
    setSelectedMembershipIds((current) => {
      const next = new Set(current);
      filteredMemberships.forEach((membership) => next.add(membership.item_id));
      return Array.from(next);
    });
  };

  const clearSelectedMemberships = () => {
    setSelectedMembershipIds([]);
  };

  const applyMembershipBatch = async (kind: "pin" | "unpin") => {
    if (!selectedRoomId || !roomDetail || membershipActionInFlight) return;
    const targets = selectedMemberships.filter((membership) => (kind === "pin" ? !membership.pinned : membership.pinned));
    if (targets.length === 0) return;

    setMembershipAction({ kind });
    try {
      for (const membership of targets) {
        if (kind === "pin") {
          await api.pinPalaceItem(selectedRoomId, membership.item_id);
        } else {
          await api.unpinPalaceItem(selectedRoomId, membership.item_id);
        }
      }
      await refreshRoom();
      setSelectedMembershipIds((current) => current.filter((itemId) => !targets.some((target) => target.item_id === itemId)));
      toast.success(`${kind === "pin" ? "Pinned" : "Unpinned"} ${membershipLabel(targets.length)}.`);
    } catch (err) {
      await refreshRoom();
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setMembershipAction(null);
    }
  };

  const handlePin = async (itemId: string) => {
    if (!selectedRoomId || membershipActionInFlight) return;
    setMembershipAction({ kind: "pin", itemId });
    try {
      await api.pinPalaceItem(selectedRoomId, itemId);
      await refreshRoom();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setMembershipAction(null);
    }
  };

  const handleUnpin = async (itemId: string) => {
    if (!selectedRoomId || membershipActionInFlight) return;
    setMembershipAction({ kind: "unpin", itemId });
    try {
      await api.unpinPalaceItem(selectedRoomId, itemId);
      await refreshRoom();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setMembershipAction(null);
    }
  };

  if (loadingOverview && !overview && !overviewError) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-sm text-zinc-400">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading Palace…
      </div>
    );
  }

  if (overviewError && !overview) {
    return (
      <StatePanel
        icon={RefreshCw}
        variant="error"
        title="Palace could not load."
        description={overviewError}
        action={
          <button
            type="button"
            onClick={() => void loadOverview()}
            className="rounded-full border border-rose-700/40 px-4 py-2 text-sm font-medium text-rose-50 transition hover:border-rose-500/60 hover:bg-rose-950/40"
          >
            Try again
          </button>
        }
      />
    );
  }

  const hasRooms = Boolean(overview && overview.wings.length > 0);
  const showNoSourceState = syncSources.length === 0;
  const showPrebuildState = !showNoSourceState && !hasRooms;
  const roomFinderResultLabel = selectedWing
    ? `${visibleRoomsForSelectedWing.length} of ${selectedWing.rooms.length} rooms`
    : "No rooms";

  const renderRoomFinder = (layout: "mobile" | "desktop") => (
    <div
      className={`rounded-2xl border border-zinc-800 bg-zinc-950/80 p-3 ${
        layout === "mobile" ? "mt-3" : "mb-2"
      }`}
    >
      <div className={`flex gap-2 ${layout === "mobile" ? "flex-col sm:flex-row" : "flex-col"}`}>
        <label className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
          <span className="sr-only">Find rooms by name or summary</span>
          <input
            value={roomFinderQuery}
            onChange={(event) => setRoomFinderQuery(event.target.value)}
            placeholder="Find a room"
            aria-label="Find rooms by name or summary"
            className="sb-input py-2 pl-9 text-sm"
          />
        </label>
        <label className={layout === "mobile" ? "sm:w-44" : "w-full"}>
          <span className="sr-only">Sort rooms</span>
          <select
            value={roomSort}
            onChange={(event) => setRoomSort(event.target.value as RoomSort)}
            aria-label="Sort rooms"
            className="sb-input cursor-pointer py-2 text-sm"
          >
            {ROOM_SORT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="mt-2 text-xs text-zinc-500" aria-live="polite">
        {roomFinderResultLabel}
        {selectedRoomHiddenByFinder ? " shown. Selected room stays open." : " shown."}
      </p>
    </div>
  );

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Organization"
        title="Palace"
        description="Inspect how the library is organized, see what is still indexing, and trace why retrieval chose a room."
        meta={
          <>
            <span className="sb-chip sb-chip-inactive">Indexed generation {overview?.indexed_generation ?? 0}</span>
            <span className="sb-chip sb-chip-inactive">Backlog {overview?.backlog_generation ?? 0}</span>
          </>
        }
        actions={
          <>
            <button onClick={() => navigate("/palace/control-tower")} className="sb-button-secondary">
              Open control tower
            </button>
            <button onClick={handleRunPalace} disabled={showNoSourceState} className="sb-button-primary">
              Run Palace now
            </button>
          </>
        }
      />

      {overview?.state_banner ? <PalaceStateBanner banner={overview.state_banner} /> : null}
      {overviewError && overview ? (
        <StatePanel
          icon={RefreshCw}
          compact
          variant="error"
          title="Palace refresh failed."
          description={overviewError}
          action={
            <button
              type="button"
              onClick={() => void loadOverview()}
              className="rounded-full border border-rose-700/40 px-4 py-2 text-sm font-medium text-rose-50 transition hover:border-rose-500/60 hover:bg-rose-950/40"
            >
              Reload Palace
            </button>
          }
        />
      ) : null}

      {hasRooms ? (
        <nav
          aria-label="Palace wing and room navigation"
          className="rounded-3xl border border-zinc-800 bg-zinc-950 p-4 xl:hidden"
        >
          <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
            <Compass className="h-4 w-4" />
            Wing to room
          </div>
          <div className="mt-3 -mx-1 flex gap-2 overflow-x-auto px-1 pb-1" aria-label="Palace wings">
            {overview?.wings.map((wing) => (
              <button
                key={wing.id}
                type="button"
                onClick={() => {
                  setSelectedWingId(wing.id);
                  setSelectedRoomId(wing.rooms[0]?.id ?? null);
                }}
                aria-pressed={selectedWing?.id === wing.id}
                className={`min-w-[11rem] cursor-pointer rounded-2xl border px-4 py-3 text-left transition ${
                  selectedWing?.id === wing.id
                    ? "border-zinc-500 bg-zinc-900 text-white"
                    : "border-zinc-900 bg-zinc-950 text-zinc-300 hover:border-zinc-700 hover:text-white"
                }`}
              >
                <span className="block truncate text-sm font-medium">{wing.name}</span>
                <span className="mt-1 block text-xs text-zinc-500">
                  {wing.room_count} rooms, {wing.item_count} drawers
                </span>
              </button>
            ))}
          </div>

          {selectedWing ? (
            <div className="mt-4">
              <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
                <Map className="h-4 w-4" />
                Rooms
              </div>
              {renderRoomFinder("mobile")}
              <div className="mt-3 -mx-1 flex gap-2 overflow-x-auto px-1 pb-1" aria-label={`Rooms in ${selectedWing.name}`}>
                {visibleRoomsForSelectedWing.length === 0 ? (
                  <div className="min-w-[13rem] rounded-2xl border border-dashed border-zinc-800 bg-zinc-950 px-4 py-5 text-sm text-zinc-500">
                    No rooms match this finder.
                  </div>
                ) : (
                  visibleRoomsForSelectedWing.map((room) => (
                    <button
                      key={room.id}
                      type="button"
                      onClick={() => selectRoom(room.id)}
                      aria-pressed={selectedRoomId === room.id}
                      className={`min-w-[13rem] cursor-pointer rounded-2xl border px-4 py-3 text-left transition ${
                        selectedRoomId === room.id
                          ? "border-emerald-700/40 bg-emerald-950/20 text-white"
                          : "border-zinc-900 bg-zinc-950 text-zinc-300 hover:border-zinc-700 hover:text-white"
                      }`}
                    >
                      <span className="block truncate text-sm font-medium">{room.name}</span>
                      <span className="mt-1 block text-xs text-zinc-500">{room.item_count} drawers</span>
                      <span className="mt-2 inline-flex rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                        {room.snapshot_status.status}
                      </span>
                    </button>
                  ))
                )}
              </div>
            </div>
          ) : null}
        </nav>
      ) : null}

      {showNoSourceState ? (
        <div className="sb-panel sb-panel-padding">
          <p className="text-sm font-medium text-zinc-100">Palace has no corpus source yet.</p>
          <p className="mt-2 max-w-xl text-sm text-zinc-400">
            Connect a local folder or repo first. Palace only becomes a place after it has something real to organize.
          </p>
          <button
            onClick={() => navigate("/palace/control-tower")}
            className="sb-button-secondary mt-4"
          >
            Connect a source
          </button>
        </div>
      ) : null}

      {showPrebuildState ? (
        <div className="sb-panel sb-panel-padding">
          <p className="text-sm font-medium text-zinc-100">Sources are connected. Palace has not built rooms yet.</p>
          <p className="mt-2 max-w-xl text-sm text-zinc-400">
            Start a Palace run to turn the synced corpus into wings, rooms, summaries, and tunnels.
          </p>
          <button
            onClick={handleRunPalace}
            className="sb-button-primary mt-4"
          >
            Build the Palace
          </button>
        </div>
      ) : null}

      {hasRooms ? (
        <div className="grid gap-4 xl:grid-cols-[280px,minmax(0,1fr),320px]">
          <nav
            aria-label="Desktop Palace wing and room navigation"
            className="hidden rounded-3xl border border-zinc-800 bg-zinc-950 p-4 xl:flex xl:flex-col"
          >
            <div className="mb-4 flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
              <Compass className="h-4 w-4" />
              Wings
            </div>
            <div className="space-y-1.5" aria-label="Palace wings">
              {overview?.wings.map((wing) => (
                <div key={wing.id} className="rounded-2xl border border-zinc-900 bg-zinc-950">
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedWingId(wing.id);
                      setSelectedRoomId(wing.rooms[0]?.id ?? null);
                    }}
                    aria-expanded={selectedWing?.id === wing.id}
                    className={`flex w-full cursor-pointer items-center gap-3 rounded-2xl px-3 py-3 text-left transition ${
                      selectedWing?.id === wing.id
                        ? "bg-zinc-900 text-white"
                        : "text-zinc-300 hover:bg-zinc-900/70 hover:text-white"
                    }`}
                  >
                    <ChevronRight
                      className={`h-4 w-4 shrink-0 text-zinc-500 transition-transform ${
                        selectedWing?.id === wing.id ? "rotate-90" : ""
                      }`}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{wing.name}</span>
                      <span className="mt-1 block text-xs text-zinc-500">
                        {wing.room_count} rooms, {wing.item_count} drawers
                      </span>
                    </span>
                  </button>

                  {selectedWing?.id === wing.id ? (
                    <div className="space-y-1.5 border-t border-zinc-900 px-2 py-2" aria-label={`Rooms in ${wing.name}`}>
                      {renderRoomFinder("desktop")}
                      {visibleRoomsForSelectedWing.length === 0 ? (
                        <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-950 px-3 py-4 text-sm text-zinc-500">
                          No rooms match this finder.
                        </div>
                      ) : (
                        visibleRoomsForSelectedWing.map((room) => (
                          <button
                            key={room.id}
                            type="button"
                            onClick={() => selectRoom(room.id)}
                            aria-pressed={selectedRoomId === room.id}
                            className={`w-full cursor-pointer rounded-xl border px-3 py-2.5 text-left transition ${
                              selectedRoomId === room.id
                                ? "border-emerald-700/40 bg-emerald-950/20 text-white"
                                : "border-transparent text-zinc-400 hover:border-zinc-800 hover:bg-zinc-900/70 hover:text-white"
                            }`}
                          >
                            <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0">
                                <p className="truncate text-sm font-medium">{room.name}</p>
                                <p className="mt-1 text-xs text-zinc-500">{room.item_count} drawers</p>
                              </div>
                              <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                                {room.snapshot_status.status}
                              </span>
                            </div>
                          </button>
                        ))
                      )}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
            <div className="min-h-0 flex-1" aria-hidden="true" />
          </nav>

          <section className="rounded-3xl border border-zinc-800 bg-zinc-950 p-5">
            {loadingRoom ? (
              <div className="flex h-80 items-center justify-center text-sm text-zinc-400">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Loading room…
              </div>
            ) : roomError ? (
              <StatePanel
                icon={RefreshCw}
                compact
                variant="error"
                title="This room is unavailable right now."
                description={roomError}
                action={
                  <button
                    type="button"
                    onClick={() => void retryRoom()}
                    className="rounded-full border border-rose-700/40 px-4 py-2 text-sm font-medium text-rose-50 transition hover:border-rose-500/60 hover:bg-rose-950/40"
                  >
                    Reload room
                  </button>
                }
              />
            ) : roomDetail ? (
              <div className="space-y-5">
                <div className="flex flex-col gap-3 border-b border-zinc-800 pb-4 lg:flex-row lg:items-start lg:justify-between">
                  <div>
                    <p className="text-xs uppercase tracking-[0.25em] text-zinc-500">{roomDetail.wing_name}</p>
                    {editingRoomName ? (
                      <form onSubmit={handleRenameRoom} className="mt-2 flex max-w-2xl flex-col gap-2 sm:flex-row">
                        <label className="sr-only" htmlFor="room-name">
                          Room name
                        </label>
                        <input
                          id="room-name"
                          value={roomNameDraft}
                          onChange={(event) => setRoomNameDraft(event.target.value)}
                          className="sb-input py-2 text-base font-semibold sm:text-lg"
                          autoFocus
                        />
                        <div className="flex gap-2">
                          <button
                            type="submit"
                            disabled={renamingRoom || !roomNameDraft.trim()}
                            className="sb-button-primary px-3"
                            aria-label="Save room name"
                          title="Save room name"
                        >
                            {renamingRoom ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <Check className="h-4 w-4" />
                            )}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setRoomNameDraft(roomDetail.room.name);
                              setEditingRoomName(false);
                            }}
                            disabled={renamingRoom}
                            className="sb-button-secondary px-3"
                            aria-label="Cancel rename"
                            title="Cancel rename"
                          >
                            <X className="h-4 w-4" />
                          </button>
                        </div>
                      </form>
                    ) : (
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <h2 className="text-2xl font-semibold text-zinc-100">{roomDetail.room.name}</h2>
                        <button
                          type="button"
                          onClick={() => {
                            setRoomNameDraft(roomDetail.room.name);
                            setEditingRoomName(true);
                          }}
                          disabled={roomDetail.room.state === "redirected"}
                          className="sb-button-ghost px-2 py-1.5"
                          aria-label="Rename room"
                          title={roomDetail.room.state === "redirected" ? "Redirected rooms cannot be renamed" : "Rename room"}
                        >
                          <Pencil className="h-4 w-4" />
                        </button>
                      </div>
                    )}
                    <p className="mt-2 max-w-3xl text-xs text-zinc-500">
                      Stable key stays <span className="font-mono text-zinc-400">{roomDetail.room.stable_key}</span>
                    </p>
                    <p className="mt-2 max-w-3xl text-sm leading-6 text-zinc-300">
                      {roomDetail.room.summary ?? "This room is still waiting for a fresh snapshot."}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <PalaceFreshnessPill label="Memberships" freshness={roomDetail.room.membership_status} />
                    <PalaceFreshnessPill label="Snapshot" freshness={roomDetail.room.snapshot_status} />
                    <PalaceFreshnessPill label="Tunnels" freshness={roomDetail.room.tunnel_status} />
                    <ProvenanceDrawer
                      compact
                      triggerLabel="Room evidence"
                      provenance={{
                        title: roomDetail.room.name,
                        subtitle: "Room, wing, freshness, membership, and tunnel evidence for this Palace room.",
                        kind: "room",
                        summary: roomDetail.room.summary,
                        room: {
                          name: roomDetail.room.name,
                          wing: roomDetail.wing_name,
                          scope: roomDetail.room.stable_key,
                        },
                        scores: [
                          { label: "Memberships", value: roomDetail.room.item_count },
                          { label: "Membership freshness", value: roomDetail.room.membership_status.status },
                          { label: "Snapshot freshness", value: roomDetail.room.snapshot_status.status },
                          { label: "Tunnel freshness", value: roomDetail.room.tunnel_status.status },
                        ],
                        relationships: roomDetail.memberships.slice(0, 8).map((membership) => ({
                          item_id: membership.item_id,
                          title: membership.title,
                          source_type: membership.source_type,
                          relationship: membership.membership_source === "pinned" ? "pinned_membership" : "auto_membership",
                          confidence: membership.membership_source === "pinned" ? 1 : 0.75,
                        })),
                        metadata: [
                          { label: "Room state", value: roomDetail.room.state },
                          { label: "Stable key", value: roomDetail.room.stable_key },
                          { label: "Tunnels", value: roomDetail.tunnels.length },
                        ],
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => void refreshRoom()}
                      className="sb-button-secondary px-3 py-2"
                      aria-label="Refresh room editor"
                      title="Refresh room editor"
                    >
                      <RefreshCw className="h-4 w-4" />
                    </button>
                  </div>
                </div>

                {roomDetail.banner ? <PalaceStateBanner banner={roomDetail.banner} /> : null}

                <form onSubmit={handleSubmitQuery} className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
                  <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
                    <Route className="h-4 w-4" />
                    Query this room
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {SCOPE_OPTIONS.map((option) => (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() => {
                          setScopeType(option.value);
                          if (option.value === "tenant_shared") {
                            setScopeKey("");
                          }
                        }}
                        className={`rounded-full border px-3 py-1.5 text-xs transition ${
                          scopeType === option.value
                            ? "border-emerald-700/50 bg-emerald-950/40 text-emerald-200"
                            : "border-zinc-700 bg-zinc-950 text-zinc-400 hover:border-zinc-500 hover:text-white"
                        }`}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                  <div className="mt-3 flex flex-col gap-2 lg:flex-row">
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Ask Palace to retrieve from this room first…"
                      className="flex-1 rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                    />
                    {scopeType !== "tenant_shared" ? (
                      <input
                        value={scopeKey}
                        onChange={(event) => setScopeKey(event.target.value)}
                        placeholder={SCOPE_PLACEHOLDER[scopeType]}
                        className="w-full rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500 lg:w-44"
                      />
                    ) : null}
                    <button
                      type="submit"
                      disabled={!query.trim() || submittingQuery}
                      className="rounded-2xl border border-emerald-700/50 bg-emerald-950/40 px-4 py-3 text-sm text-emerald-200 transition hover:border-emerald-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {submittingQuery ? "Tracing…" : "Trace retrieval"}
                    </button>
                  </div>
                  <p className="mt-3 text-xs text-zinc-500">
                    Scope stays visible because it changes what memory you are actually verifying.
                  </p>
                </form>

                {trace?.trace.fallback_used ? (
                  <div className="flex flex-col gap-3 rounded-2xl border border-amber-700/40 bg-amber-950/20 px-4 py-4 lg:flex-row lg:items-center lg:justify-between">
                    <div>
                      <p className="text-sm font-medium text-amber-100">Fallback stayed in Palace.</p>
                      <p className="mt-1 text-xs text-amber-200/80">
                        The room context stayed visible while retrieval widened to the broader library.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => navigate("/browse")}
                      className="rounded-full border border-amber-700/40 bg-zinc-950 px-3 py-1.5 text-xs text-amber-100 transition hover:border-amber-500 hover:text-white"
                    >
                      Open in Library
                    </button>
                  </div>
                ) : null}

                <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr),minmax(0,0.85fr)]">
                  <div className="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
                    <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
                      <Milestone className="h-4 w-4" />
                      Representative drawers
                    </div>
                    <div className="space-y-3">
                      {roomDetail.representative_items.map((item) => (
                        <div key={item.item_id} className="rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3">
                          <div className="flex items-start gap-3">
                            <SourceIcon sourceType={item.source_type} className="mt-0.5 h-4 w-4 shrink-0" />
                            <div className="min-w-0">
                              <p className="text-sm font-medium text-zinc-100">{item.title}</p>
                              {item.summary ? <p className="mt-1 text-xs text-zinc-400">{item.summary}</p> : null}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
                    <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
                      <Compass className="h-4 w-4" />
                      Tunnels
                    </div>
                    <div className="space-y-2">
                      {roomDetail.tunnels.length === 0 ? (
                        <p className="text-sm text-zinc-500">No nearby rooms yet.</p>
                      ) : (
                        roomDetail.tunnels.map((tunnel) => (
                          <button
                            key={`${tunnel.room_id}-${tunnel.tunnel_type}`}
                            onClick={() => selectRoom(tunnel.room_id)}
                            className="flex w-full items-center justify-between rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3 text-left text-sm text-zinc-200 transition hover:border-zinc-700 hover:text-white"
                          >
                            <div>
                              <p className="font-medium">{tunnel.room_name}</p>
                              <p className="mt-1 text-xs text-zinc-500">
                                {tunnel.tunnel_type} • strength {(tunnel.strength * 100).toFixed(0)}%
                              </p>
                            </div>
                            <ChevronRight className="h-4 w-4 text-zinc-500" />
                          </button>
                        ))
                      )}
                    </div>
                  </div>
                </div>

                <div className="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
                  <div className="flex flex-col gap-4 border-b border-zinc-800 pb-4">
                    <div>
                      <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
                        <SlidersHorizontal className="h-4 w-4" />
                        Advanced room editor
                      </div>
                      <div className="mt-3 grid gap-2 sm:grid-cols-3">
                        <div className="rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3" aria-label={membershipLabel(roomEditorStats.total, "room")}>
                          <p className="text-[11px] uppercase tracking-[0.18em] text-zinc-500">Memberships</p>
                          <p className="mt-1 text-xl font-semibold text-zinc-100">{roomEditorStats.total}</p>
                        </div>
                        <div className="rounded-2xl border border-sky-800/50 bg-sky-950/20 px-4 py-3" aria-label={membershipLabel(roomEditorStats.pinned, "pinned")}>
                          <p className="text-[11px] uppercase tracking-[0.18em] text-sky-300/70">Pinned</p>
                          <p className="mt-1 text-xl font-semibold text-sky-100">{roomEditorStats.pinned}</p>
                        </div>
                        <div className="rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3" aria-label={membershipLabel(roomEditorStats.auto, "auto routed")}>
                          <p className="text-[11px] uppercase tracking-[0.18em] text-zinc-500">Auto routed</p>
                          <p className="mt-1 text-xl font-semibold text-zinc-100">{roomEditorStats.auto}</p>
                        </div>
                      </div>
                    </div>
                    <div className="rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
                          <GitMerge className="h-4 w-4" />
                          Consolidation review
                        </div>
                        <span className="rounded-full border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-300">
                          {roomConsolidationCandidates.length
                            ? `${roomConsolidationCandidates.length} candidate${roomConsolidationCandidates.length === 1 ? "" : "s"}`
                            : "No candidates"}
                        </span>
                      </div>
                      {roomDetail.redirect_target ? (
                        <div className="mt-3 rounded-xl border border-sky-800/50 bg-sky-950/20 px-3 py-2 text-sm text-sky-100">
                          <p className="font-medium">Redirect target: {roomDetail.redirect_target.name}</p>
                          <p className="mt-1 font-mono text-xs text-sky-200/70">{roomDetail.redirect_target.stable_key}</p>
                        </div>
                      ) : null}
                      {roomConsolidationCandidates.length ? (
                        <div className="mt-3 space-y-2">
                          {roomConsolidationCandidates.map((candidate) => {
                            const counterpart = candidateCounterpart(candidate, roomDetail.room.id);
                            return (
                              <div
                                key={`${candidate.room_id}-${candidate.candidate_room_id}`}
                                className="rounded-xl border border-amber-900/50 bg-amber-950/10 px-3 py-3"
                              >
                                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                                  <div className="min-w-0">
                                    <p className="text-sm font-medium text-zinc-100">{counterpart.name}</p>
                                    <p className="mt-1 truncate font-mono text-xs text-zinc-500">{counterpart.stableKey}</p>
                                    {candidate.reasons.length ? (
                                      <p className="mt-2 text-xs text-zinc-400">{candidate.reasons.join(", ")}</p>
                                    ) : null}
                                  </div>
                                  <div className="flex shrink-0 items-center gap-2">
                                    <span className="rounded-full border border-amber-700/50 bg-amber-950/40 px-2.5 py-1 text-xs font-medium text-amber-100">
                                      {formatCandidateScore(candidate.score)}
                                    </span>
                                    <button
                                      type="button"
                                      onClick={() => selectRoom(counterpart.id)}
                                      className="rounded-full border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-200 transition hover:border-zinc-500 hover:text-white"
                                    >
                                      Inspect
                                    </button>
                                  </div>
                                </div>
                                {candidate.shared_tags.length ? (
                                  <div className="mt-3 flex flex-wrap gap-1.5">
                                    {candidate.shared_tags.slice(0, 6).map((tag) => (
                                      <span key={tag} className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400">
                                        {tag}
                                      </span>
                                    ))}
                                  </div>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="mt-3 text-sm text-zinc-500">
                          Control Tower is not surfacing a merge or redirect conflict for this room.
                        </p>
                      )}
                    </div>
                    <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={allVisibleMembershipsSelected ? clearSelectedMemberships : selectVisibleMemberships}
                          disabled={filteredMemberships.length === 0 || membershipActionInFlight}
                          className="rounded-full border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs font-medium text-zinc-200 transition hover:border-zinc-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {allVisibleMembershipsSelected ? "Clear selection" : "Select visible"}
                        </button>
                        <span className="text-xs text-zinc-500" aria-live="polite">
                          {selectedMembershipIds.length
                            ? `${membershipLabel(selectedMembershipIds.length, "selected")} · ${selectedAutoCount} can pin · ${selectedPinnedCount} can unpin`
                            : "No memberships selected"}
                        </span>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={() => void applyMembershipBatch("pin")}
                          disabled={selectedAutoCount === 0 || membershipActionInFlight}
                          className="rounded-full border border-sky-700/40 bg-sky-950/30 px-3 py-2 text-xs font-medium text-sky-200 transition hover:border-sky-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {membershipAction?.kind === "pin" && !membershipAction.itemId ? "Pinning…" : "Pin selected"}
                        </button>
                        <button
                          type="button"
                          onClick={() => void applyMembershipBatch("unpin")}
                          disabled={selectedPinnedCount === 0 || membershipActionInFlight}
                          className="rounded-full border border-rose-700/40 bg-rose-950/30 px-3 py-2 text-xs font-medium text-rose-200 transition hover:border-rose-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {membershipAction?.kind === "unpin" && !membershipAction.itemId ? "Unpinning…" : "Unpin selected"}
                        </button>
                      </div>
                    </div>
                    <div className="flex flex-col gap-2 sm:flex-row 2xl:justify-end">
                      <label className="relative min-w-0 sm:w-72">
                        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                        <span className="sr-only">Search room memberships</span>
                        <input
                          value={membershipSearch}
                          onChange={(event) => setMembershipSearch(event.target.value)}
                          placeholder="Search memberships"
                          className="sb-input py-2 pl-9"
                        />
                      </label>
                      <div className="flex rounded-2xl border border-zinc-800 bg-zinc-950 p-1">
                        {MEMBERSHIP_FILTERS.map((filter) => (
                          <button
                            key={filter.value}
                            type="button"
                            onClick={() => setMembershipFilter(filter.value)}
                            className={`min-w-16 rounded-xl px-3 py-2 text-xs font-medium transition ${
                              membershipFilter === filter.value
                                ? "bg-zinc-800 text-zinc-50"
                                : "text-zinc-500 hover:text-zinc-200"
                            }`}
                          >
                            {filter.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>
                  <div className="mt-4 space-y-2">
                    {filteredMemberships.length === 0 ? (
                      <div className="rounded-2xl border border-dashed border-zinc-800 bg-zinc-950 px-4 py-6 text-sm text-zinc-500">
                        No memberships match the current editor filters.
                      </div>
                    ) : (
                      filteredMemberships.map((membership) => (
                        <div
                          key={`${membership.item_id}-${membership.membership_source}`}
                          className={`flex flex-col gap-3 rounded-2xl border px-4 py-3 transition lg:flex-row lg:items-start lg:justify-between ${
                            selectedMembershipIdSet.has(membership.item_id)
                              ? "border-sky-700/50 bg-sky-950/20"
                              : "border-zinc-800 bg-zinc-950"
                          }`}
                        >
                          <div className="flex min-w-0 gap-3">
                            <label className="mt-1 inline-flex h-5 w-5 shrink-0 items-center justify-center">
                              <span className="sr-only">Select {membership.title}</span>
                              <input
                                type="checkbox"
                                checked={selectedMembershipIdSet.has(membership.item_id)}
                                onChange={(event) => toggleMembershipSelection(membership.item_id, event.target.checked)}
                                disabled={membershipActionInFlight}
                                className="h-4 w-4 accent-sky-500 rounded border-zinc-700 bg-zinc-900 text-sky-500 focus:ring-2 focus:ring-sky-500 focus:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50"
                              />
                            </label>
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <p className="text-sm font-medium text-zinc-100">{membership.title}</p>
                                <span
                                  className={`rounded-full border px-2 py-1 text-[11px] ${
                                    membership.pinned
                                      ? "border-sky-700/40 bg-sky-950/40 text-sky-200"
                                      : "border-zinc-700 bg-zinc-900 text-zinc-400"
                                  }`}
                                >
                                  {membership.pinned ? "Pinned" : "Auto"}
                                </span>
                                <span className="rounded-full border border-zinc-800 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-500">
                                  {membership.membership_kind}
                                </span>
                              </div>
                              {membership.summary ? (
                                <p className="mt-1 text-xs text-zinc-400">{membership.summary}</p>
                              ) : null}
                            </div>
                          </div>
                          {membership.pinned ? (
                            <button
                              type="button"
                              onClick={() => handleUnpin(membership.item_id)}
                              disabled={membershipActionInFlight}
                              className="rounded-full border border-rose-700/40 bg-rose-950/30 px-3 py-1.5 text-xs text-rose-200 transition hover:border-rose-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {membershipAction?.kind === "unpin" && membershipAction.itemId === membership.item_id ? "Unpinning…" : "Unpin"}
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => handlePin(membership.item_id)}
                              disabled={membershipActionInFlight}
                              className="rounded-full border border-sky-700/40 bg-sky-950/30 px-3 py-1.5 text-xs text-sky-200 transition hover:border-sky-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {membershipAction?.kind === "pin" && membershipAction.itemId === membership.item_id ? "Pinning…" : "Pin to this room"}
                            </button>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="flex h-80 items-center justify-center text-sm text-zinc-500">
                Select a room to inspect its meaning, freshness, and retrieval trace.
              </div>
            )}
          </section>

          <aside className="rounded-3xl border border-zinc-800 bg-zinc-950 p-4">
            <div className="mb-4 flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
              <Route className="h-4 w-4" />
              Retrieval trace
            </div>
            {trace?.trace.status_banner ? <PalaceStateBanner banner={trace.trace.status_banner} /> : null}
            {trace ? (
              <div className="space-y-4">
                {trace.trace.completeness_warning ? (
                  <p className="rounded-2xl border border-amber-700/40 bg-amber-950/30 px-4 py-3 text-sm text-amber-100">
                    {trace.trace.completeness_warning}
                  </p>
                ) : null}

                <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3 text-sm text-zinc-300">
                  <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Scope and wing</p>
                  <p className="mt-2 text-zinc-100">
                    {trace.trace.requested_scope_type}
                    {trace.trace.requested_scope_key ? `:${trace.trace.requested_scope_key}` : ""}
                  </p>
                  <p className="mt-2 text-xs text-zinc-400">
                    Wing: {trace.trace.selected_wing ?? "Global fallback"}
                  </p>
                  {trace.trace.candidate_rooms.length > 0 ? (
                    <p className="mt-2 text-xs text-zinc-400">
                      Candidate rooms: {trace.trace.candidate_rooms.join(", ")}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-2">
                  {trace.trace.steps.map((step) => (
                    <div key={step.title} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
                      <p className="text-sm font-medium text-zinc-100">{step.title}</p>
                      <p className="mt-1 text-xs text-zinc-400">{step.detail}</p>
                    </div>
                  ))}
                </div>

                {(trace.trace.ranking_traces?.length ?? 0) > 0 ? (
                  <div className="space-y-2">
                    <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Result provenance</p>
                    {trace.trace.ranking_traces.map((rankingTrace) => (
                      <div key={rankingTrace.route} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <p className="text-sm font-medium capitalize text-zinc-100">{formatTraceRoute(rankingTrace.route)}</p>
                          <p className="text-xs text-zinc-500">
                            {rankingTrace.result_count} shown
                            {typeof rankingTrace.candidate_count === "number" ? ` of ${rankingTrace.candidate_count} candidates` : ""}
                          </p>
                        </div>
                        <div className="mt-3 space-y-2">
                          {rankingTrace.results.slice(0, 5).map((row) => (
                            <div key={`${rankingTrace.route}-${row.rank}-${row.item_id ?? "candidate"}`} className="rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2">
                              <div className="flex flex-wrap items-center gap-2 text-xs">
                                <span className="font-medium text-zinc-200">#{row.rank}</span>
                                <span className="rounded-full border border-emerald-700/40 bg-emerald-950/30 px-2 py-0.5 text-emerald-100">
                                  {row.artifact_provenance_label ?? "Broad corpus item"}
                                </span>
                                <span className="rounded-full border border-sky-700/40 bg-sky-950/30 px-2 py-0.5 text-sky-100">
                                  {row.retrieved_scope_label ?? "general"}
                                </span>
                                {row.source_type ? (
                                  <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-zinc-300">{row.source_type}</span>
                                ) : null}
                                {formatScore(row.adjusted_score) ? (
                                  <span className="ml-auto text-zinc-500">score {formatScore(row.adjusted_score)}</span>
                                ) : null}
                                <ProvenanceDrawer
                                  compact
                                  triggerLabel="Evidence"
                                  provenance={{
                                    title: row.item_id ? `Ranked item ${row.item_id}` : `Rank ${row.rank} candidate`,
                                    subtitle: `Ranking evidence from ${formatTraceRoute(rankingTrace.route)}.`,
                                    kind: row.artifact_provenance_type ? "derived_artifact" : "retrieval_trace",
                                    itemId: row.item_id,
                                    sourceType: row.source_type,
                                    room: {
                                      name: roomDetail?.room.name,
                                      wing: trace.trace.selected_wing,
                                      scope: row.retrieved_scope_label ?? row.retrieved_scope_key ?? trace.trace.requested_scope_type,
                                    },
                                    scores: [
                                      { label: "Rank", value: `#${row.rank}` },
                                      ...(typeof row.base_score === "number" ? [{ label: "Base score", value: row.base_score }] : []),
                                      ...(typeof row.adjusted_score === "number" ? [{ label: "Adjusted score", value: row.adjusted_score, tone: "good" as const }] : []),
                                    ],
                                    traceSteps: [
                                      { title: "Route", detail: formatTraceRoute(rankingTrace.route) },
                                      ...(rankingTrace.query_intent ? [{ title: "Query intent", detail: rankingTrace.query_intent }] : []),
                                      ...(row.derived_artifact_keys?.length ? [{ title: "Derived artifact keys", detail: row.derived_artifact_keys.join(", ") }] : []),
                                    ],
                                    metadata: [
                                      { label: "Artifact provenance", value: row.artifact_provenance_label ?? row.artifact_provenance_type },
                                      { label: "Retrieved scope", value: row.retrieved_scope_label ?? row.retrieved_scope_key },
                                      { label: "Candidates", value: rankingTrace.candidate_count },
                                      { label: "Results shown", value: rankingTrace.result_count },
                                    ],
                                  }}
                                />
                              </div>
                              {row.derived_artifact_keys && row.derived_artifact_keys.length > 1 ? (
                                <p className="mt-1 text-xs text-amber-200">
                                  Multiple derived markers: {row.derived_artifact_keys.join(", ")}
                                </p>
                              ) : null}
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : null}

                <div className="space-y-2">
                  {trace.results.map((result) => (
                    <div key={`${result.item_id}-${result.chunk_text.slice(0, 20)}`} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
                      <div className="flex items-start justify-between gap-3">
                        <p className="min-w-0 text-sm font-medium text-zinc-100">{result.title}</p>
                        <ProvenanceDrawer
                          compact
                          triggerLabel="Evidence"
                          provenance={{
                            title: result.title,
                            subtitle: "Retrieved result evidence for the active Palace room query.",
                            kind: result.artifact_citation ? "derived_artifact" : "retrieval_trace",
                            itemId: result.item_id,
                            sourceType: result.source_type,
                            sourceUrl: result.source_url,
                            summary: result.summary,
                            excerpt: result.chunk_text,
                            artifact: result.artifact_citation,
                            room: {
                              name: roomDetail?.room.name,
                              wing: trace.trace.selected_wing,
                              scope: trace.trace.requested_scope_key ?? trace.trace.requested_scope_type,
                            },
                            scores: [{ label: "Result score", value: result.score, tone: result.score >= 0.7 ? "good" : "default" }],
                            traceSteps: trace.trace.steps,
                          }}
                        />
                      </div>
                      {result.summary ? <p className="mt-1 text-xs text-zinc-400">{result.summary}</p> : null}
                      <blockquote className="mt-2 border-l border-zinc-700 pl-3 text-xs italic text-zinc-500">
                        {result.chunk_text}
                      </blockquote>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-4 text-sm text-zinc-400">
                <p className="text-zinc-100">Trace stays quiet until you ask a question or hit a noteworthy state.</p>
                <p>
                  Start in a room. When you query, this rail will explain the active scope, wing chosen, room expansion, fallback behavior, and provenance.
                </p>
              </div>
            )}
          </aside>
        </div>
      ) : null}
    </div>
  );
}
