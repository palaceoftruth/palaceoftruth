import { expect, test } from "@playwright/test";

const testScopeCatalog = [
  { value: "read", label: "Read memory", description: "Read memory and audit surfaces.", category: "memory" },
  { value: "write", label: "Write memory", description: "Create tenant-shared memory.", category: "memory" },
  { value: "write:agent", label: "Write agent scope", description: "Create agent-scoped memory.", category: "memory" },
  { value: "write:workspace", label: "Write workspace scope", description: "Create workspace-scoped memory.", category: "memory" },
  { value: "write:session", label: "Write session scope", description: "Create session-scoped memory.", category: "memory" },
  { value: "admin", label: "Admin tools", description: "Call administrative MCP operations.", category: "admin" },
  { value: "local_only", label: "Local-only client", description: "Marks a local runtime client.", category: "guardrail" },
  { value: "destructive_prohibited", label: "No destructive tools", description: "Prohibit destructive operations.", category: "guardrail" },
  { value: "capture:write", label: "Capture writes", description: "Create browser captures.", category: "capture" },
  { value: "capture:job:read", label: "Capture job reads", description: "Poll browser capture jobs.", category: "capture" },
];

async function mockPalaceOverview(
  page: Parameters<typeof test>[0]["page"],
  overview: unknown,
  syncSources: unknown[] = [],
  controlTower: unknown = { consolidation: { candidate_count: 0, candidates: [] } },
) {
  await page.route("**/api/v1/palace/sync-sources", async (route) => {
    await route.fulfill({ json: syncSources });
  });
  await page.route("**/api/v1/palace/control-tower", async (route) => {
    await route.fulfill({ json: controlTower });
  });
  await page.route("**/api/v1/palace", async (route) => {
    await route.fulfill({ json: overview });
  });
}

async function mockPalaceControlTower(page: Parameters<typeof test>[0]["page"], tower: unknown) {
  await page.route("**/api/v1/palace/control-tower", async (route) => {
    await route.fulfill({ json: tower });
  });
  await page.route("**/api/v1/palace/mcp-clients", async (route) => {
    await route.fulfill({
      json: {
        tenant_id: "default",
        clients: [],
        scope_catalog: testScopeCatalog,
        config_snippets: {
          codex_stdio_toml: "[mcp_servers.palaceoftruth-memory]\\ncommand = \"uv\"",
          http_oauth_toml: "[mcp_servers.palaceoftruth-memory]\\nbearer_token_env_var = \"PALACEOFTRUTH_MCP_BEARER_TOKEN\"",
          oauth_token_command: "read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET",
          legacy_api_key_toml: "X-API-Key = \"set-from-your-secret-manager\"",
          secret_handling_note: "The client_secret is returned once.",
        },
      },
    });
  });
  await page.route("**/api/v1/palace/mcp-grants", async (route) => {
    await route.fulfill({ json: { tenant_id: "default", grants: [] } });
  });
  await page.route("**/api/v1/palace/source-resources", async (route) => {
    await route.fulfill({ json: { resources: [], total: 0 } });
  });
}

async function expectNoHorizontalOverflow(page: Parameters<typeof test>[0]["page"]) {
  const overflow = await page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const documentOverflow = document.documentElement.scrollWidth - viewportWidth;
    const offenders = Array.from(document.body.querySelectorAll<HTMLElement>("*"))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return { element, rect, style };
      })
      .filter(({ rect, style }) => (
        rect.width > 0
        && rect.height > 0
        && style.display !== "none"
        && style.visibility !== "hidden"
        && rect.right > viewportWidth + 1
      ))
      .slice(0, 10)
      .map(({ element, rect }) => ({
        tag: element.tagName.toLowerCase(),
        className: element.className,
        text: element.textContent?.replace(/\s+/g, " ").trim().slice(0, 120) ?? "",
        left: Math.round(rect.left),
        right: Math.round(rect.right),
        width: Math.round(rect.width),
      }));

    return { documentOverflow, offenders };
  });

  expect(overflow.documentOverflow, JSON.stringify(overflow.offenders, null, 2)).toBeLessThanOrEqual(1);
  expect(overflow.offenders).toEqual([]);
}

test.describe("Palace smoke", () => {
  test("palace route shows truthful empty state", async ({ page }) => {
    await mockPalaceOverview(page, {
      tenant_id: "default",
      dirty_generation: 0,
      indexed_generation: 0,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings: [],
    });

    await page.goto(`/palace?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Palace" })).toBeVisible();
    await expect(page.getByText("Palace has no corpus source yet.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Open control tower" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect a source" })).toBeVisible();
  });

  test("palace retrieval trace shows result provenance", async ({ page }) => {
    const room = {
      id: "8d7f6215-71e8-4bd7-9fef-a192d93d1d84",
      wing_id: "7a2e4ff9-3397-4c5c-8f04-096a242c2c33",
      name: "Memory Reliability",
      stable_key: "infra-code-agents:memory-reliability",
      state: "active",
      item_count: 2,
      summary: "Traceable memory retrieval examples.",
      membership_status: { status: "fresh", generation: 3, target_generation: 3, message: "Fresh" },
      snapshot_status: { status: "fresh", generation: 3, target_generation: 3, message: "Fresh" },
      tunnel_status: { status: "fresh", generation: 3, target_generation: 3, message: "Fresh" },
      redirect_room_id: null,
    };

    await mockPalaceOverview(page, {
      tenant_id: "default",
      dirty_generation: 3,
      indexed_generation: 3,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings: [
        {
          id: room.wing_id,
          slug: "infra-code-agents",
          name: "Infra / Code / Agents",
          room_count: 1,
          item_count: room.item_count,
          rooms: [room],
        },
      ],
    });
    await page.route("**/api/v1/palace/rooms/*", async (route) => {
      await route.fulfill({
        json: {
          room,
          wing_name: "Infra / Code / Agents",
          banner: null,
          representative_items: [],
          tunnels: [],
          memberships: [],
          redirect_target: null,
        },
      });
    });
    await page.route("**/api/v1/palace/retrieve", async (route) => {
      await route.fulfill({
        json: {
          routed_room_id: room.id,
          redirected_from_room_id: null,
          trace: {
            requested_scope_type: "workspace",
            requested_scope_key: "palaceoftruth",
            selected_wing: "Infra / Code / Agents",
            candidate_rooms: ["Memory Reliability"],
            expanded_rooms: [],
            fallback_used: false,
            completeness_warning: null,
            steps: [{ title: "Scoped retrieval", detail: "Retrieved drawers from the selected scope." }],
            ranking_traces: [
              {
                route: "room_scoped",
                candidate_count: 2,
                result_count: 2,
                routing: {},
                results: [
                  {
                    rank: 1,
                    item_id: "81f81e5a-732e-4443-9a9e-86f7d603ecf4",
                    source_type: "note",
                    artifact_provenance_type: "canonical_memory",
                    artifact_provenance_label: "Canonical memory",
                    derived_artifact_keys: [],
                    retrieved_scope_type: "workspace",
                    retrieved_scope_key: "palaceoftruth",
                    retrieved_scope_label: "workspace/palaceoftruth",
                    adjusted_score: 0.912345,
                    adjustments: {},
                  },
                  {
                    rank: 2,
                    item_id: "92fd1b47-838e-475a-9a5d-11aac5bb3a28",
                    source_type: "note",
                    artifact_provenance_type: "wakeup_brief",
                    artifact_provenance_label: "Wake-up brief",
                    derived_artifact_keys: ["wakeup_brief"],
                    retrieved_scope_type: "tenant_shared",
                    retrieved_scope_key: null,
                    retrieved_scope_label: "tenant_shared",
                    adjusted_score: 0.712345,
                    adjustments: {},
                  },
                ],
              },
            ],
          },
          results: [
            {
              item_id: "81f81e5a-732e-4443-9a9e-86f7d603ecf4",
              title: "Scoped Codex memory",
              source_type: "note",
              score: 0.912345,
              summary: "A canonical memory entry.",
              chunk_text: "A bounded excerpt appears here.",
            },
          ],
          total: 1,
        },
      });
    });

    await page.goto(`/palace?e2e=${Date.now()}`);
    await page.getByPlaceholder("Ask Palace to retrieve from this room first…").fill("memory provenance");
    await page.getByRole("button", { name: "Trace retrieval" }).click();

    await expect(page.getByText("Result provenance")).toBeVisible();
    await expect(page.getByText("room scoped")).toBeVisible();
    await expect(page.getByText("Canonical memory", { exact: true })).toBeVisible();
    await expect(page.getByText("workspace/palaceoftruth")).toBeVisible();
    await expect(page.getByText("Wake-up brief")).toBeVisible();
    await expect(page.locator("span", { hasText: "tenant_shared" })).toBeVisible();
    await expect(page.getByText("score 0.912")).toBeVisible();
  });

  test("palace room editor can rename a room and batch curate memberships", async ({ page }) => {
    const roomId = "8d7f6215-71e8-4bd7-9fef-a192d93d1d84";
    const wingId = "7a2e4ff9-3397-4c5c-8f04-096a242c2c33";
    const freshness = {
      status: "fresh",
      generation: 3,
      target_generation: 3,
      message: "Current with the latest indexed Palace generation.",
    };
    const room = {
      id: roomId,
      wing_id: wingId,
      name: "Pricing Narrative",
      stable_key: "product-growth:pricing-narrative",
      state: "active",
      item_count: 2,
      summary: "Founder pricing notes live here.",
      membership_status: freshness,
      snapshot_status: freshness,
      tunnel_status: freshness,
      redirect_room_id: null,
    };
    let currentRoom = { ...room };
    let patchBody: Record<string, unknown> | null = null;
    let memberships = [
      {
        item_id: "item-1",
        title: "Pricing memo",
        source_type: "note",
        summary: "Initial package strategy",
        membership_source: "pinned",
        membership_kind: "primary",
        pinned: true,
      },
      {
        item_id: "item-2",
        title: "Sales call notes",
        source_type: "webpage",
        summary: "Trial objections from a customer call",
        membership_source: "auto",
        membership_kind: "secondary",
        pinned: false,
      },
    ];
    const pinRequests: string[] = [];
    const unpinRequests: string[] = [];

    await mockPalaceOverview(page, {
      tenant_id: "default",
      dirty_generation: 3,
      indexed_generation: 3,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings: [
        {
          id: wingId,
          slug: "product-growth",
          name: "Product / Growth",
          room_count: 1,
          item_count: 2,
          rooms: [currentRoom],
        },
      ],
    }, [
      {
        id: "source-1",
        name: "Vault",
        root_path: "/vault",
        source_kind: "folder",
        credential_type: "none",
        has_stored_credential: false,
        status: "active",
        scan_interval_seconds: 900,
      },
    ], {
      consolidation: {
        candidate_count: 1,
        candidates: [
          {
            room_id: roomId,
            room_name: "Pricing Narrative",
            room_stable_key: "product-growth:pricing-narrative",
            candidate_room_id: "91dd7d55-76c5-453f-917f-302b4f639bc8",
            candidate_room_name: "Pricing Objections",
            candidate_stable_key: "product-growth:pricing-objections",
            wing_id: wingId,
            wing_name: "Product / Growth",
            score: 0.81,
            reasons: ["shared tags", "overlapping drawers"],
            shared_tags: ["pricing", "sales"],
            shared_drawer_item_ids: ["item-2"],
          },
        ],
      },
    });
    await page.route(`**/api/v1/palace/rooms/${roomId}/pins`, async (route) => {
      const body = route.request().postDataJSON() as { item_id: string };
      pinRequests.push(body.item_id);
      memberships = memberships.map((membership) => (
        membership.item_id === body.item_id
          ? { ...membership, membership_source: "pinned", pinned: true }
          : membership
      ));
      await route.fulfill({ status: 204 });
    });
    await page.route(`**/api/v1/palace/rooms/${roomId}/pins/*`, async (route) => {
      const itemId = route.request().url().split("/").pop() ?? "";
      unpinRequests.push(itemId);
      memberships = memberships.map((membership) => (
        membership.item_id === itemId
          ? { ...membership, membership_source: "auto", pinned: false }
          : membership
      ));
      await route.fulfill({ status: 204 });
    });
    await page.route(`**/api/v1/palace/rooms/${roomId}`, async (route) => {
      if (route.request().method() === "PATCH") {
        patchBody = route.request().postDataJSON() as Record<string, unknown>;
        currentRoom = { ...currentRoom, name: String(patchBody.name) };
      }
      await route.fulfill({
        json: {
          room: currentRoom,
          wing_name: "Product / Growth",
          banner: null,
          representative_items: [
            {
              item_id: "item-1",
              title: "Pricing memo",
              source_type: "note",
              summary: "Initial package strategy",
              membership_source: "pinned",
              pinned: true,
            },
          ],
          tunnels: [],
          memberships,
          redirect_target: null,
        },
      });
    });

    await page.goto(`/palace?e2e=${Date.now()}`);
    await expect(page.getByRole("heading", { name: "Pricing Narrative" })).toBeVisible();
    await expect(page.getByText("Stable key stays product-growth:pricing-narrative")).toBeVisible();

    await page.getByRole("button", { name: "Rename room" }).click();
    await page.getByLabel("Room name", { exact: true }).fill("Investor Diligence");
    await page.getByRole("button", { name: "Save room name" }).click();

    await expect(page.getByRole("heading", { name: "Investor Diligence" })).toBeVisible();
    await expect(page.getByText("Stable key stays product-growth:pricing-narrative")).toBeVisible();
    expect(patchBody).toEqual({ name: "Investor Diligence" });
    await page.getByRole("button", { name: "Dismiss" }).click();

    await expect(page.getByText("Advanced room editor")).toBeVisible();
    await expect(page.getByText("Consolidation review")).toBeVisible();
    await expect(page.getByText("Pricing Objections")).toBeVisible();
    await expect(page.getByText("81%")).toBeVisible();
    await expect(page.getByLabel("2 room memberships")).toBeVisible();
    await expect(page.getByLabel("1 pinned membership")).toBeVisible();
    await page.getByLabel("Search room memberships").fill("sales");
    await expect(page.getByText("Sales call notes", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Unpin", exact: true })).not.toBeVisible();
    await expect(page.getByRole("button", { name: "Pin to this room" })).toBeVisible();
    await page.getByLabel("Select Sales call notes").check();
    await expect(page.getByText("1 selected membership · 1 can pin · 0 can unpin")).toBeVisible();
    await page.getByRole("button", { name: "Pin selected", exact: true }).click();
    await expect(page.getByLabel("2 pinned memberships")).toBeVisible();
    await expect(page.getByText("Sales call notes", { exact: true })).toBeVisible();
    expect(pinRequests).toEqual(["item-2"]);
    await page.getByRole("button", { name: "Select visible" }).click();
    await expect(page.getByText("1 selected membership · 0 can pin · 1 can unpin")).toBeVisible();
    await page.getByRole("button", { name: "Unpin selected", exact: true }).click();
    await expect(page.getByLabel("1 pinned membership")).toBeVisible();
    expect(unpinRequests).toEqual(["item-2"]);
    await page.getByRole("button", { name: "Pinned" }).click();
    await expect(page.getByText("No memberships match the current editor filters.")).toBeVisible();
  });

  test("palace mobile first viewport includes wing and room navigation", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 740 });
    const freshness = {
      status: "fresh",
      generation: 5,
      target_generation: 5,
      message: "Current with the latest indexed Palace generation.",
    };
    const room = {
      id: "room-mobile",
      wing_id: "wing-mobile",
      name: "Launch Room",
      stable_key: "projects:launch-room",
      state: "active",
      item_count: 4,
      summary: "Launch planning lives here.",
      membership_status: freshness,
      snapshot_status: freshness,
      tunnel_status: freshness,
      redirect_room_id: null,
    };

    await mockPalaceOverview(page, {
      tenant_id: "default",
      dirty_generation: 5,
      indexed_generation: 5,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings: [
        {
          id: "wing-mobile",
          slug: "projects",
          name: "Projects",
          room_count: 1,
          item_count: 4,
          rooms: [room],
        },
      ],
    }, [
      {
        id: "source-1",
        name: "Vault",
        root_path: "/vault",
        source_kind: "folder",
        credential_type: "none",
        has_stored_credential: false,
        status: "active",
        scan_interval_seconds: 900,
      },
    ]);
    await page.route("**/api/v1/palace/rooms/room-mobile", async (route) => {
      await route.fulfill({
        json: {
          room,
          wing_name: "Projects",
          banner: null,
          representative_items: [],
          tunnels: [],
          memberships: [],
          redirect_target: null,
        },
      });
    });

    await page.goto(`/palace?e2e=${Date.now()}`);

    const navigator = page.getByRole("navigation", { name: "Palace wing and room navigation" });
    await expect(navigator).toBeVisible();
    await expect(navigator).toBeInViewport();
    await expect(navigator.getByRole("button", { name: /Projects 1 rooms, 4 drawers/ })).toBeVisible();
    await expect(navigator.getByRole("button", { name: /Launch Room 4 drawers fresh/ })).toBeVisible();
  });

  test("palace desktop wing navigation expands one sidebar section for 15 wings", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const freshness = {
      status: "fresh",
      generation: 9,
      target_generation: 9,
      message: "Current with the latest indexed Palace generation.",
    };
    const wings = Array.from({ length: 15 }, (_, index) => {
      const ordinal = String(index + 1).padStart(2, "0");
      const room = {
        id: `room-${ordinal}`,
        wing_id: `wing-${ordinal}`,
        name: `Room ${ordinal}`,
        stable_key: `wing-${ordinal}:room-${ordinal}`,
        state: "active",
        item_count: index + 1,
        summary: `Room ${ordinal} summary.`,
        membership_status: freshness,
        snapshot_status: freshness,
        tunnel_status: freshness,
        redirect_room_id: null,
      };
      return {
        id: `wing-${ordinal}`,
        slug: `wing-${ordinal}`,
        name: `Wing ${ordinal}`,
        room_count: 1,
        item_count: index + 1,
        rooms: [room],
      };
    });

    await mockPalaceOverview(page, {
      tenant_id: "default",
      dirty_generation: 9,
      indexed_generation: 9,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings,
    }, [
      {
        id: "source-1",
        name: "Vault",
        root_path: "/vault",
        source_kind: "folder",
        credential_type: "none",
        has_stored_credential: false,
        status: "active",
        scan_interval_seconds: 900,
      },
    ]);
    await page.route("**/api/v1/palace/rooms/*", async (route) => {
      const roomId = route.request().url().split("/").pop() ?? "";
      const room = wings.flatMap((wing) => wing.rooms).find((candidate) => candidate.id === roomId) ?? wings[0].rooms[0];
      await route.fulfill({
        json: {
          room,
          wing_name: wings.find((wing) => wing.id === room.wing_id)?.name ?? "Wing 01",
          banner: null,
          representative_items: [],
          tunnels: [],
          memberships: [],
          redirect_target: null,
        },
      });
    });

    await page.goto(`/palace?e2e=${Date.now()}`);

    const navigator = page.getByRole("navigation", { name: "Desktop Palace wing and room navigation" });
    const roomPanel = page.getByRole("heading", { name: "Room 01" }).locator("xpath=ancestor::section[1]");
    const tracePanel = page.getByText("Retrieval trace").locator("xpath=ancestor::aside[1]");
    await expect(navigator).toBeVisible();
    const firstWing = navigator.getByRole("button", { name: /Wing 01 1 rooms, 1 drawers/ });
    const finalWing = navigator.getByRole("button", { name: /Wing 15 1 rooms, 15 drawers/ });
    await expect(firstWing).toHaveAttribute("aria-expanded", "true");
    await expect(navigator.getByLabel("Rooms in Wing 01")).toBeVisible();
    await expect(navigator.getByLabel("Rooms in Wing 02")).not.toBeVisible();
    await expect(navigator).toHaveCSS("overflow-y", "visible");

    const navigatorBox = await navigator.boundingBox();
    const roomPanelBox = await roomPanel.boundingBox();
    const tracePanelBox = await tracePanel.boundingBox();
    expect(navigatorBox).not.toBeNull();
    expect(roomPanelBox).not.toBeNull();
    expect(tracePanelBox).not.toBeNull();
    expect(Math.abs(navigatorBox!.height - roomPanelBox!.height)).toBeLessThan(2);
    expect(Math.abs(navigatorBox!.height - tracePanelBox!.height)).toBeLessThan(2);

    await finalWing.scrollIntoViewIfNeeded();
    await finalWing.click();

    await expect(finalWing).toHaveAttribute("aria-expanded", "true");
    await expect(firstWing).toHaveAttribute("aria-expanded", "false");
    await expect(navigator.getByLabel("Rooms in Wing 15")).toBeVisible();
    await expect(navigator.getByRole("button", { name: /Room 15 15 drawers fresh/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Room 15" })).toBeVisible();
  });

  test("palace room finder searches and sorts a large selected wing without changing room context", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const fresh = {
      status: "fresh",
      generation: 12,
      target_generation: 12,
      message: "Current with the latest indexed Palace generation.",
    };
    const indexing = {
      status: "indexing",
      generation: 11,
      target_generation: 12,
      message: "Snapshot is still catching up.",
    };
    const stale = {
      status: "stale",
      generation: 9,
      target_generation: 12,
      message: "Snapshot needs a Palace refresh.",
    };
    const rooms = [
      {
        id: "room-alpha",
        wing_id: "wing-a",
        name: "Alpha Strategy",
        stable_key: "wing-a:alpha-strategy",
        state: "active",
        item_count: 1,
        summary: "The currently selected operator room.",
        membership_status: fresh,
        snapshot_status: fresh,
        tunnel_status: fresh,
        redirect_room_id: null,
      },
      {
        id: "room-pricing",
        wing_id: "wing-a",
        name: "Growth Pricing",
        stable_key: "wing-a:growth-pricing",
        state: "active",
        item_count: 7,
        summary: "Packaging experiments and pricing objections.",
        membership_status: indexing,
        snapshot_status: indexing,
        tunnel_status: indexing,
        redirect_room_id: null,
      },
      {
        id: "room-reports",
        wing_id: "wing-a",
        name: "Reports Archive",
        stable_key: "wing-a:reports-archive",
        state: "active",
        item_count: 9,
        summary: "Weekly evidence reports that need a refresh.",
        membership_status: stale,
        snapshot_status: stale,
        tunnel_status: stale,
        redirect_room_id: null,
      },
      {
        id: "room-support",
        wing_id: "wing-a",
        name: "Support Themes",
        stable_key: "wing-a:support-themes",
        state: "active",
        item_count: 4,
        summary: "Customer support themes and evidence.",
        membership_status: fresh,
        snapshot_status: fresh,
        tunnel_status: fresh,
        redirect_room_id: null,
      },
    ];

    await mockPalaceOverview(page, {
      tenant_id: "default",
      dirty_generation: 12,
      indexed_generation: 12,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings: [
        {
          id: "wing-a",
          slug: "wing-a",
          name: "Wing A",
          room_count: rooms.length,
          item_count: rooms.reduce((sum, room) => sum + room.item_count, 0),
          rooms,
        },
      ],
    }, [
      {
        id: "source-1",
        name: "Vault",
        root_path: "/vault",
        source_kind: "folder",
        credential_type: "none",
        has_stored_credential: false,
        status: "active",
        scan_interval_seconds: 900,
      },
    ]);
    await page.route("**/api/v1/palace/rooms/*", async (route) => {
      const roomId = route.request().url().split("/").pop() ?? "";
      const room = rooms.find((candidate) => candidate.id === roomId) ?? rooms[0];
      await route.fulfill({
        json: {
          room,
          wing_name: "Wing A",
          banner: null,
          representative_items: [],
          tunnels: [],
          memberships: [],
          redirect_target: null,
        },
      });
    });

    await page.goto(`/palace?e2e=${Date.now()}`);

    const navigator = page.getByRole("navigation", { name: "Desktop Palace wing and room navigation" });
    const roomsRegion = navigator.getByLabel("Rooms in Wing A");
    await expect(page.getByRole("heading", { name: "Alpha Strategy" })).toBeVisible();

    await roomsRegion.getByLabel("Find rooms by name or summary").fill("pricing");
    await expect(roomsRegion.getByText("1 of 4 rooms shown. Selected room stays open.")).toBeVisible();
    await expect(roomsRegion.getByRole("button", { name: /Growth Pricing 7 drawers indexing/ })).toBeVisible();
    await expect(roomsRegion.getByRole("button", { name: /Alpha Strategy/ })).not.toBeVisible();
    await expect(page.getByRole("heading", { name: "Alpha Strategy" })).toBeVisible();

    await roomsRegion.getByRole("button", { name: /Growth Pricing 7 drawers indexing/ }).click();
    await expect(page.getByRole("heading", { name: "Growth Pricing" })).toBeVisible();

    await roomsRegion.getByLabel("Find rooms by name or summary").fill("");
    await roomsRegion.getByLabel("Sort rooms").selectOption("drawers");
    await expect(roomsRegion.getByRole("button").nth(0)).toContainText("Reports Archive");
    await roomsRegion.getByLabel("Sort rooms").selectOption("freshness");
    await expect(roomsRegion.getByRole("button").nth(0)).toContainText("Reports Archive");
    await expect(roomsRegion.getByRole("button").nth(1)).toContainText("Growth Pricing");
  });

  test("control tower route shows sync and run surfaces", async ({ page }) => {
    await mockPalaceControlTower(page, {
      tenant_id: "default",
      dirty_generation: 0,
      indexed_generation: 0,
      backlog_generation: 0,
      active_palace_run: null,
      memory_health: {
        queued: 0,
        processing: 0,
        failed: 0,
        retryable: 0,
        recent_jobs: [],
      },
      webhook_health: {
        configured: 1,
        pending: 0,
        terminal: 1,
        failed_jobs: 0,
        retryable_jobs: 0,
        recent_jobs: [
          {
            job_id: "webhook-job-1",
            title: "Webhook launch note",
            job_type: "note",
            status: "complete",
            terminal: true,
            error_message: null,
            created_at: "2026-04-23T10:00:00Z",
            completed_at: "2026-04-23T10:01:00Z",
          },
        ],
      },
      fact_registry: {
        active: 0,
        superseded: 0,
        distinct_sources: 0,
        last_extracted_at: null,
        recent_facts: [],
      },
      diary_rollups: {
        fresh: 0,
        stale: 0,
        expected_through_day: "2026-04-22",
        last_refreshed_at: null,
        recent_rollups: [],
      },
      wakeup_briefs: {
        fresh: 0,
        stale: 0,
        generated_for_day: null,
        last_refreshed_at: null,
        recent_briefs: [],
      },
      sync_sources: [],
      sync_runs: [],
      palace_runs: [],
    });

    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Palace Control Tower" })).toBeVisible();
    await expect(page.getByText("Add sync source")).toBeVisible();
    await expect(page.getByText("Webhook delivery health")).toBeVisible();
    await expect(page.getByText("Webhook launch note")).toBeVisible();
    await page.getByText("Wake-up brief freshness").scrollIntoViewIfNeeded();
    await expect(page.getByText("Wake-up brief freshness")).toBeVisible();
    await expect(page.getByRole("heading", { name: /No wake-up briefs yet/i })).toBeVisible();
    await page.getByText("Wakeup trust health").scrollIntoViewIfNeeded();
    await expect(page.getByText("Wakeup trust health")).toBeVisible();
    await expect(page.getByRole("heading", { name: /No source trust counts yet/i })).toBeVisible();
    await page.getByText("Diary rollup freshness").scrollIntoViewIfNeeded();
    await expect(page.getByText("Diary rollup freshness")).toBeVisible();
    await expect(page.getByRole("heading", { name: /No diary rollups yet/i })).toBeVisible();
    await page.getByText("Fact registry freshness").scrollIntoViewIfNeeded();
    await expect(page.getByText("Fact registry freshness")).toBeVisible();
    await expect(page.getByRole("heading", { name: /No sync sources/i })).toBeVisible();
    await expect(page.getByRole("button", { name: "Start Palace run" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Start Palace run" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Add the first source" })).toBeVisible();
  });

  test("control tower shows loading and source trust error states", async ({ page }) => {
    await page.route("**/api/v1/palace/control-tower", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 150));
      await route.fulfill({
        json: {
          tenant_id: "default",
          dirty_generation: 0,
          indexed_generation: 0,
          backlog_generation: 0,
          active_palace_run: null,
          memory_health: { queued: 0, processing: 0, failed: 0, retryable: 0, recent_jobs: [] },
          webhook_health: { configured: 0, pending: 0, terminal: 0, failed_jobs: 0, retryable_jobs: 0, recent_jobs: [] },
          fact_registry: { active: 0, superseded: 0, distinct_sources: 0, last_extracted_at: null, recent_facts: [] },
          diary_rollups: { fresh: 0, stale: 0, expected_through_day: null, last_refreshed_at: null, recent_rollups: [] },
          wakeup_briefs: { fresh: 0, stale: 0, generated_for_day: null, last_refreshed_at: null, recent_briefs: [] },
          source_trust_health: {
            status: "error",
            total_contexts: 0,
            source_backed: 0,
            generated_unpromoted: 0,
            stale_missing: 0,
            policy_limited: 0,
            unknown: 0,
            recent_warnings: [],
            error_message: "Source trust counts failed; MCP wakeup remains usable.",
          },
          sync_sources: [],
          sync_runs: [],
          palace_runs: [],
        },
      });
    });
    await page.route("**/api/v1/palace/mcp-clients", async (route) => {
      await route.fulfill({
        json: {
          tenant_id: "default",
          clients: [],
          config_snippets: {
            codex_stdio_toml: "[mcp_servers.palaceoftruth-memory]\\ncommand = \"uv\"",
            http_oauth_toml: "[mcp_servers.palaceoftruth-memory]",
            oauth_token_command: "read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET",
            legacy_api_key_toml: "X-API-Key = \"set-from-your-secret-manager\"",
            secret_handling_note: "The client_secret is returned once.",
          },
        },
      });
    });

    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await expect(page.getByText("Loading control tower")).toBeVisible();
    await page.getByText("Wakeup trust health").scrollIntoViewIfNeeded();
    await expect(page.getByRole("heading", { name: "Source trust counts failed." })).toBeVisible();
    await expect(page.getByText("MCP wakeup remains usable")).toBeVisible();
  });

  test("control tower shows watched-source freshness, audited controls, and canonical history", async ({ page }) => {
    const resource = {
      id: "source-1",
      kind: "http",
      canonical_url: "https://docs.example.test/guide",
      freshness: "due",
      status: "active",
      refresh_policy: "adaptive",
      refresh_slo_seconds: 3600,
      last_http_status: 200,
      has_etag: true,
      has_last_modified: true,
      consecutive_failures: 2,
      robots_decision: "allowed",
      robots_cached_at: "2026-07-18T19:00:00Z",
      published_at: "2026-07-01T12:00:00Z",
      captured_at: "2026-07-18T18:00:00Z",
      last_verified_at: "2026-07-18T18:30:00Z",
      content_changed_at: null,
      last_checked_at: "2026-07-18T18:30:00Z",
      last_success_at: "2026-07-18T18:30:00Z",
      next_due_at: "2026-07-18T19:30:00Z",
      backoff_until: "2026-07-18T20:00:00Z",
      current_source_record_id: "record-1",
      last_successful_source_record_id: "record-1",
    };
    await mockPalaceControlTower(page, {
      tenant_id: "default", dirty_generation: 0, indexed_generation: 0, backlog_generation: 0, active_palace_run: null,
      memory_health: { queued: 0, processing: 0, failed: 0, retryable: 0, recent_jobs: [] },
      webhook_health: { configured: 0, pending: 0, terminal: 0, failed_jobs: 0, retryable_jobs: 0, recent_jobs: [] },
      fact_registry: { active: 0, superseded: 0, distinct_sources: 0, last_extracted_at: null, recent_facts: [] },
      diary_rollups: { fresh: 0, stale: 0, expected_through_day: null, last_refreshed_at: null, recent_rollups: [] },
      wakeup_briefs: { fresh: 0, stale: 0, generated_for_day: null, last_refreshed_at: null, recent_briefs: [] },
      sync_sources: [], sync_runs: [], palace_runs: [],
    });
    await page.unroute("**/api/v1/palace/source-resources");
    await page.route("**/api/v1/palace/source-resources/source-1", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: { ...resource, aliases: [{ id: "alias-1", signal: "canonical", decision: "conflict", normalized_url: "https://docs.example.test/old", final_url: null, canonical_signal_url: "https://docs.example.test/guide", observed_at: "2026-07-18T18:00:00Z" }], audit_events: [{ id: "audit-1", event_kind: "operator_policy_updated", previous_status: "active", next_status: "active", previous_refresh_policy: "interval", next_refresh_policy: "adaptive", recorded_at: "2026-07-18T18:30:00Z" }] } });
      } else {
        await route.fulfill({ json: resource });
      }
    });
    await page.route("**/api/v1/palace/source-resources", async (route) => {
      await route.fulfill({ json: { resources: [resource], total: 1 } });
    });
    await page.addInitScript(() => localStorage.setItem("sb:browser_api_key", "test-key"));
    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);
    const card = page.getByRole("article", { name: /Watched source https:\/\/docs.example.test\/guide/ });
    await expect(card).toContainText("Verified");
    await expect(card).toContainText("Published");
    await expect(card.getByRole("button", { name: "Refresh" })).toBeVisible();
    await expect(card.getByRole("button", { name: "Pause" })).toBeVisible();
    await card.getByRole("button", { name: "View history" }).click();
    await expect(card).toContainText("1 canonical conflict");
    await expect(card).toContainText("https://docs.example.test/old");
    await expect(card).toContainText("Operator audit trail");
    await expect(card).toContainText("operator policy updated");
    if (process.env.UI_EVIDENCE_DIR) {
      await page.screenshot({ path: `${process.env.UI_EVIDENCE_DIR}/sar-1206-desktop.png`, fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(250); // Let the desktop-to-mobile sidebar transition settle before capturing evidence.
      await expectNoHorizontalOverflow(page);
      await card.screenshot({ path: `${process.env.UI_EVIDENCE_DIR}/sar-1206-mobile.png` });
    }
  });

  test("control tower registers MCP agents and keeps secrets out of persistent panels", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 820 });
    let registerBody: Record<string, unknown> | null = null;
    let revokedClientId: string | null = null;

    await mockPalaceControlTower(page, {
      tenant_id: "default",
      dirty_generation: 0,
      indexed_generation: 0,
      backlog_generation: 0,
      active_palace_run: null,
      memory_health: { queued: 0, processing: 0, failed: 0, retryable: 0, recent_jobs: [] },
      webhook_health: { configured: 0, pending: 0, terminal: 0, failed_jobs: 0, retryable_jobs: 0, recent_jobs: [] },
      fact_registry: { active: 0, superseded: 0, distinct_sources: 0, last_extracted_at: null, recent_facts: [] },
      diary_rollups: { fresh: 0, stale: 0, expected_through_day: null, last_refreshed_at: null, recent_rollups: [] },
      wakeup_briefs: { fresh: 0, stale: 0, generated_for_day: null, last_refreshed_at: null, recent_briefs: [] },
      sync_sources: [],
      sync_runs: [],
      palace_runs: [],
      mcp_activity: {
        registered_clients: 1,
        recent_success: 4,
        recent_denied: 1,
        recent_error: 0,
        recent_events: [],
      },
    });

    await page.route("**/api/v1/palace/mcp-clients**", async (route) => {
      const request = route.request();
      if (request.method() === "POST" && request.url().endsWith("/register")) {
        registerBody = request.postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          json: {
            tenant_id: "default",
            client_secret: "copy-once-secret-should-not-persist",
            client: {
              id: "mcp-client-2",
              tenant_id: "default",
              client_key: "codex-remote-long-client-key-that-wraps",
              display_name: "Codex remote MCP",
              allowed_scopes: ["read", "write", "destructive_prohibited"],
              metadata: {},
              token_ttl_seconds: 3600,
              created_at: "2026-05-05T00:00:00Z",
              last_seen_at: null,
              request_count: 0,
              success_count: 0,
              denied_count: 0,
              error_count: 0,
              last_request_at: null,
              revoked_at: null,
            },
            config_snippets: {
              codex_stdio_toml: "[mcp_servers.palaceoftruth-memory]",
              http_oauth_toml: "bearer_token_env_var = \"PALACEOFTRUTH_MCP_BEARER_TOKEN\"",
              oauth_token_command: "read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET",
              legacy_api_key_toml: "X-API-Key = \"set-from-your-secret-manager\"",
              secret_handling_note: "The client_secret is returned once.",
            },
            scope_catalog: testScopeCatalog,
          },
        });
        return;
      }
      await route.fulfill({
        json: {
          tenant_id: "default",
          clients: [
            {
              id: "mcp-client-1",
              tenant_id: "default",
              client_key: "codex-remote-long-client-key-that-wraps",
              display_name: "Codex remote MCP with a long operator label",
              allowed_scopes: ["read", "write", "destructive_prohibited"],
              metadata: {},
              token_ttl_seconds: 3600,
              created_at: "2026-05-05T00:00:00Z",
              last_seen_at: "2026-05-05T00:01:00Z",
              request_count: 5,
              success_count: 4,
              denied_count: 1,
              error_count: 0,
              last_request_at: "2026-05-05T00:01:00Z",
              revoked_at: null,
            },
          ],
          scope_catalog: testScopeCatalog,
          config_snippets: {
            codex_stdio_toml: "[mcp_servers.palaceoftruth-memory]\\nPALACEOFTRUTH_API_KEY = \"set-from-your-secret-manager\"",
            http_oauth_toml: "bearer_token_env_var = \"PALACEOFTRUTH_MCP_BEARER_TOKEN\"",
            oauth_token_command: "read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET",
            legacy_api_key_toml: "X-API-Key = \"set-from-your-secret-manager\"",
            secret_handling_note: "The client_secret is returned once.",
          },
        },
      });
    });
    await page.route("**/api/v1/palace/mcp-clients/*/revoke", async (route) => {
      revokedClientId = route.request().url().split("/").at(-2) ?? null;
      await route.fulfill({ json: { tenant_id: "default", revoked: true, client: {} } });
    });

    page.on("dialog", (dialog) => void dialog.accept());
    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await expect(page.getByText("Register MCP agent")).toBeVisible();
    await expect(page.getByText("Write workspace scope")).toBeVisible();
    await expect(page.getByText("Create workspace-scoped memory.")).toBeVisible();
    await expect(page.getByText("Capture writes")).toBeVisible();
    await expect(page.getByText("Create browser captures.")).toBeVisible();
    await expect(page.getByText("Codex remote MCP with a long operator label")).toBeVisible();
    await page.getByLabel("MCP client key").fill("codex-remote-long-client-key-that-wraps");
    await page.getByRole("button", { name: "Register agent" }).click();

    await expect.poll(() => registerBody).not.toBeNull();
    expect(registerBody).toMatchObject({
      client_key: "codex-remote-long-client-key-that-wraps",
      allowed_scopes: ["read", "write", "destructive_prohibited"],
    });
    await expect(page.getByText("Copy once secret")).toBeVisible();
    await expect(page.getByText("read -rsp")).toBeVisible();
    await expect(page.getByText("bearer_token_env_var")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "Revoke" }).first().click();
    await expect.poll(() => revokedClientId).toBe("mcp-client-1");
  });

  test("control tower lists constrained delegated grants and revokes one", async ({ page }, testInfo) => {
    let revokedGrantId: string | null = null;
    await mockPalaceControlTower(page, {
      tenant_id: "default", dirty_generation: 0, indexed_generation: 0, backlog_generation: 0, active_palace_run: null,
      memory_health: { queued: 0, processing: 0, failed: 0, retryable: 0, recent_jobs: [] },
      webhook_health: { configured: 0, pending: 0, terminal: 0, failed_jobs: 0, retryable_jobs: 0, recent_jobs: [] },
      fact_registry: { active: 0, superseded: 0, distinct_sources: 0, last_extracted_at: null, recent_facts: [] },
      diary_rollups: { fresh: 0, stale: 0, expected_through_day: null, last_refreshed_at: null, recent_rollups: [] },
      wakeup_briefs: { fresh: 0, stale: 0, generated_for_day: null, last_refreshed_at: null, recent_briefs: [] },
      sync_sources: [], sync_runs: [], palace_runs: [],
    });
    await page.route("**/api/v1/palace/mcp-grants**", async (route) => {
      if (route.request().method() === "POST") {
        revokedGrantId = route.request().url().split("/").at(-2) ?? null;
        await route.fulfill({ json: { tenant_id: "default", revoked: true, grant: {} } });
        return;
      }
      await route.fulfill({ json: {
        tenant_id: "default",
        grants: [{
          id: "grant-1", client_id: "client-1", client_key: "nebulaios", client_name: "NebulaiOS", resource: "https://api.palace.sarvent.cloud/mcp",
          scopes: ["read", "write:agent"], agent_scope_keys: ["nebulaios"], workspace_scope_keys: ["palaceoftruth"], authorized_by: "tenant-admin-browser", revoked_at: null,
        }],
      } });
    });
    page.on("dialog", (dialog) => void dialog.accept());
    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await expect(page.getByText("Delegated grants")).toBeVisible();
    await expect(page.getByText("NebulaiOS", { exact: true })).toBeVisible();
    await expect(page.getByText(/agents nebulaios.*workspaces palaceoftruth/)).toBeVisible();
    const delegatedGrantPanel = page.getByText("Delegated grants").locator("..").locator("..");
    await delegatedGrantPanel.screenshot({ path: testInfo.outputPath("sar-1142-delegated-grant-desktop.png") });
    await page.screenshot({ path: testInfo.outputPath("sar-1142-control-tower-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 820 });
    await expectNoHorizontalOverflow(page);
    await delegatedGrantPanel.screenshot({ path: testInfo.outputPath("sar-1142-delegated-grant-mobile.png") });
    await page.screenshot({ path: testInfo.outputPath("sar-1142-control-tower-mobile.png"), fullPage: true });
    await page.getByRole("button", { name: "Revoke grant" }).click();
    await expect.poll(() => revokedGrantId).toBe("grant-1");
    await expectNoHorizontalOverflow(page);
  });

  test("control tower handles long operator labels without horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 820 });
    await mockPalaceControlTower(page, {
      tenant_id: "default",
      dirty_generation: 42,
      indexed_generation: 41,
      backlog_generation: 1,
      active_palace_run: null,
      room_artifacts: {
        target_generation: 42,
        active_rooms: 13,
        blocked_rooms: 1,
        closets: { fresh: 12, stale: 1 },
        snapshots: { fresh: 11, stale: 2 },
        tunnels: { fresh: 10, stale: 3 },
      },
      consolidation: {
        candidate_count: 1,
        candidates: [
          {
            room_id: "room-long-a",
            room_name: "Enterprise Procurement Evidence Room With Very Long Name",
            room_stable_key: "enterprise-procurement:evidence-room-with-very-long-name",
            candidate_room_id: "room-long-b",
            candidate_room_name: "Procurement Redlines And Renewal Counterparty Notes",
            candidate_stable_key: "enterprise-procurement:renewal-counterparty-notes",
            wing_id: "wing-long",
            wing_name: "Enterprise Procurement",
            score: 0.91,
            reasons: ["overlapping source material", "shared purchasing committee facts"],
            shared_tags: [
              "enterprise-procurement-renewal-committee",
              "security-review",
              "pricing",
              "legal",
            ],
            shared_drawer_item_ids: ["item-1"],
          },
        ],
      },
      worker_backpressure: {
        generated_at: "2026-04-23T10:00:00Z",
        queues: [
          {
            key: "memory-durable-writes",
            label: "Memory durable writes with exceptionally long operator-facing queue label",
            queue_name: "arq:queue:palaceoftruth:memory:durable:writes:tenant-default:priority-high",
            functions: [
              "sync_memory_write_with_large_payload",
              "refresh_wakeup_brief_for_all_active_workspaces",
              "extract_temporal_facts_from_diary_rollup",
            ],
            queued_depth: 12,
            deferred_depth: 3,
            oldest_queued_age_seconds: 3725,
            worker_concurrency: 8,
            worker_queue_depth: 15,
            recent_completed: 9,
            recent_failed: 2,
            recent_avg_latency_seconds: 82,
            telemetry_error: "redis-cluster-primary-east-1-timeout-while-reading-llen-for-worker-queue",
          },
        ],
      },
      memory_health: {
        queued: 12,
        processing: 2,
        failed: 1,
        retryable: 1,
        recent_jobs: [
          {
            job_id: "memory-job-long",
            title: "Durable memory write for quarterly planning packet with long sourced evidence title",
            job_type: "note",
            status: "failed",
            accepted_as: "canonical",
            terminal: true,
            retriable: true,
            error_message: "Could not write item with source path /Users/operator/workspace/very/deep/path/to/private/customer/research/packet.md",
            created_at: "2026-04-23T10:00:00Z",
            completed_at: null,
            scope: {
              type: "workspace",
              key: "enterprise-procurement-renewal-room-with-extra-detail",
            },
            source: "sync_source_with_a_very_long_identifier_for_visual_regression",
          },
        ],
      },
      webhook_health: {
        configured: 2,
        pending: 1,
        terminal: 1,
        failed_jobs: 1,
        retryable_jobs: 1,
        recent_jobs: [
          {
            job_id: "webhook-job-long",
            title: "Webhook launch note with long title from automation status mirror",
            job_type: "memory_write_completed_callback",
            status: "complete",
            terminal: true,
            error_message: null,
            created_at: "2026-04-23T10:00:00Z",
            completed_at: "2026-04-23T10:01:00Z",
          },
        ],
      },
      fact_registry: {
        active: 1,
        superseded: 0,
        distinct_sources: 1,
        last_extracted_at: "2026-04-23T10:00:00Z",
        recent_facts: [
          {
            id: "fact-long",
            subject: "enterprise procurement committee",
            predicate: "requires",
            object_text: "security-review-before-annual-renewal-with-a-long-unbroken-policy-identifier",
            status: "active",
            source_item_title: "Customer renewal packet with very long source title for regression testing",
            extracted_at: "2026-04-23T10:00:00Z",
            confidence: 0.86,
          },
        ],
      },
      diary_rollups: {
        fresh: 1,
        stale: 1,
        expected_through_day: "2026-04-22",
        last_refreshed_at: "2026-04-23T10:00:00Z",
        recent_rollups: [
          {
            title: "Diary Rollup 2026-04-22 [workspace:enterprise-procurement-renewal-room]",
            scope_type: "workspace",
            scope_key: "enterprise-procurement-renewal-room",
            day: "2026-04-22",
            updated_at: "2026-04-23T10:00:00Z",
            source_count: 3,
            stale: false,
          },
        ],
      },
      wakeup_briefs: {
        fresh: 1,
        stale: 1,
        generated_for_day: "2026-04-23",
        last_refreshed_at: "2026-04-23T10:00:00Z",
        recent_briefs: [
          {
            title: "Enterprise procurement wake-up brief with long planning context title",
            scope_type: "wing",
            scope_key: "enterprise-procurement-renewal-room",
            generation: 42,
            updated_at: "2026-04-23T10:00:00Z",
            room_count: 8,
            diary_count: 3,
            fact_count: 7,
            stale: false,
          },
        ],
      },
      sync_sources: [
        {
          id: "source-long",
          name: "Private repo with a long operational source name",
          root_path: "https://github.com/palaceoftruth/palaceoftruth-private-customer-renewal-evidence",
          source_kind: "repo",
          credential_type: "deployment_github_pat",
          has_stored_credential: false,
          status: "active",
          scan_interval_seconds: 900,
          allowed_extensions: [".md", ".markdown", ".customer-renewal-evidence"],
          last_synced_at: "2026-04-23T10:00:00Z",
          last_error: "Last scan skipped an unsupported file at nested/path/with/a/very-long-file-name-that-should-wrap.md",
        },
      ],
      sync_runs: [
        {
          id: "sync-run-long",
          sync_source_id: "source-long",
          sync_source_name: "Private repo with a long operational source name",
          status: "failed",
          files_changed: 4,
          files_skipped: 2,
          items_created: 1,
          items_updated: 3,
          started_at: "2026-04-23T10:00:00Z",
          completed_at: "2026-04-23T10:01:00Z",
          error_message: "Could not parse nested/path/with/a/very-long-file-name-that-should-wrap.md",
        },
      ],
      palace_runs: [
        {
          id: "palace-run-long",
          status: "failed",
          triggered_by: "operator",
          requested_generation: 42,
          applied_generation: 41,
          attempt: 2,
          error_message: "Artifact refresh blocked by membership repair for enterprise-procurement-renewal-room",
          started_at: "2026-04-23T10:00:00Z",
          completed_at: "2026-04-23T10:01:00Z",
        },
      ],
    });

    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Palace Control Tower" })).toBeVisible();
    await expect(page.getByText("Worker backpressure")).toBeVisible();
    await expect(page.getByText("Private repo with a long operational source name").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByText("Fact registry freshness").scrollIntoViewIfNeeded();
    await expect(page.getByText("Customer renewal packet with very long source title for regression testing")).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test("control tower keeps sync source actions clear of long source metadata", async ({ page }) => {
    await mockPalaceControlTower(page, {
      tenant_id: "default",
      dirty_generation: 3,
      indexed_generation: 3,
      backlog_generation: 0,
      active_palace_run: null,
      memory_health: {
        queued: 0,
        processing: 0,
        failed: 0,
        retryable: 0,
        recent_jobs: [],
      },
      webhook_health: {
        configured: 0,
        pending: 0,
        terminal: 0,
        failed_jobs: 0,
        retryable_jobs: 0,
        recent_jobs: [],
      },
      fact_registry: {
        active: 0,
        superseded: 0,
        distinct_sources: 0,
        last_extracted_at: null,
        recent_facts: [],
      },
      diary_rollups: {
        fresh: 0,
        stale: 0,
        expected_through_day: "2026-04-22",
        last_refreshed_at: null,
        recent_rollups: [],
      },
      wakeup_briefs: {
        fresh: 0,
        stale: 0,
        generated_for_day: null,
        last_refreshed_at: null,
        recent_briefs: [],
      },
      sync_sources: [
        {
          id: "source-s3-long",
          name: "Hermes S3 evidence mirror with long operator label",
          root_path: "s3://palaceoftruth-hermes-prod-evidence/archive/tenant-default/enterprise-renewal/committee-packets",
          source_kind: "s3",
          credential_type: "none",
          has_stored_credential: false,
          status: "active",
          scan_interval_seconds: 900,
          allowed_extensions: [".md", ".markdown", ".pdf", ".customer-renewal-evidence"],
          endpoint_url: "https://s3-compatible-storage.internal.example.com/very/long/service/path/for/hermes-control-tower",
          region: "us-east-1",
          force_path_style: true,
          last_synced_at: "2026-04-23T10:00:00Z",
          last_error: null,
        },
      ],
      sync_runs: [],
      palace_runs: [],
    });

    for (const viewport of [
      { width: 1280, height: 720 },
      { width: 390, height: 820 },
    ]) {
      await page.setViewportSize(viewport);
      await page.goto(`/palace/control-tower?viewport=${viewport.width}&e2e=${Date.now()}`);

      const card = page.getByRole("article", { name: /Sync source Hermes S3 evidence mirror/ });
      const actions = card.getByRole("group", { name: /Actions for Hermes S3 evidence mirror/ });
      const endpoint = card.getByText("https://s3-compatible-storage.internal.example.com", { exact: false });
      const syncNow = card.getByRole("button", { name: "Sync now" });

      await expect(card).toBeVisible();
      await expect(actions).toBeVisible();
      await expect(endpoint).toBeVisible();
      await expect(syncNow).toBeVisible();
      await expect(syncNow).toHaveCSS("white-space", "nowrap");
      await expectNoHorizontalOverflow(page);

      const boxes = await Promise.all([
        endpoint.boundingBox(),
        actions.boundingBox(),
        syncNow.boundingBox(),
      ]);
      expect(boxes.every(Boolean), JSON.stringify({ viewport, boxes })).toBe(true);
      const [endpointBox, actionsBox, syncNowBox] = boxes as NonNullable<(typeof boxes)[number]>[];
      const endpointRect = {
        left: endpointBox.x,
        right: endpointBox.x + endpointBox.width,
        top: endpointBox.y,
        bottom: endpointBox.y + endpointBox.height,
      };
      const actionsRect = {
        left: actionsBox.x,
        right: actionsBox.x + actionsBox.width,
        top: actionsBox.y,
        bottom: actionsBox.y + actionsBox.height,
      };
      const overlaps = !(
        endpointRect.right <= actionsRect.left
        || actionsRect.right <= endpointRect.left
        || endpointRect.bottom <= actionsRect.top
        || actionsRect.bottom <= endpointRect.top
      );

      expect(overlaps, JSON.stringify({ viewport, endpointRect, actionsRect })).toBe(false);
      expect(syncNowBox.height, JSON.stringify({ viewport, syncNowBox })).toBeLessThan(34);
    }
  });

  test("control tower shows wake-up brief freshness and diary rollup coverage", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 820 });
    await mockPalaceControlTower(page, {
      tenant_id: "default",
      dirty_generation: 8,
      indexed_generation: 8,
      backlog_generation: 0,
      active_palace_run: null,
      memory_health: {
        queued: 0,
        processing: 0,
        failed: 0,
        retryable: 0,
        recent_jobs: [],
      },
      webhook_health: {
        configured: 0,
        pending: 0,
        terminal: 0,
        failed_jobs: 0,
        retryable_jobs: 0,
        recent_jobs: [],
      },
      fact_registry: {
        active: 3,
        superseded: 1,
        distinct_sources: 2,
        last_extracted_at: "2026-04-23T10:00:00Z",
        recent_facts: [],
      },
      diary_rollups: {
        fresh: 1,
        stale: 1,
        expected_through_day: "2026-04-22",
        last_refreshed_at: "2026-04-23T10:00:00Z",
        recent_rollups: [
          {
            title: "Diary Rollup 2026-04-22 [workspace:launch-pad]",
            scope_type: "workspace",
            scope_key: "launch-pad",
            day: "2026-04-22",
            updated_at: "2026-04-23T10:00:00Z",
            source_count: 2,
            stale: false,
          },
          {
            title: "Diary Rollup 2026-04-21 [session:focus-1]",
            scope_type: "session",
            scope_key: "focus-1",
            day: "2026-04-21",
            updated_at: "2026-04-23T09:45:00Z",
            source_count: 1,
            stale: true,
          },
        ],
      },
      wakeup_briefs: {
        fresh: 1,
        stale: 1,
        generated_for_day: "2026-04-23",
        last_refreshed_at: "2026-04-23T10:00:00Z",
        recent_briefs: [
          {
            title: "Tenant wake-up brief",
            scope_type: "tenant",
            scope_key: null,
            generation: 8,
            updated_at: "2026-04-23T10:00:00Z",
            room_count: 4,
            diary_count: 2,
            fact_count: 5,
            stale: false,
          },
          {
            title: "Projects wake-up brief",
            scope_type: "wing",
            scope_key: "03-projects",
            generation: 7,
            updated_at: "2026-04-23T09:45:00Z",
            room_count: 2,
            diary_count: 1,
            fact_count: 3,
            stale: true,
          },
        ],
      },
      source_trust_health: {
        status: "ready",
        total_contexts: 7,
        source_backed: 3,
        generated_unpromoted: 2,
        stale_missing: 1,
        policy_limited: 1,
        unknown: 0,
        recent_warnings: [
          {
            state: "generated_unpromoted",
            warning: "generated_artifact_without_promoted_source_support",
            count: 2,
            source_preview: "raw chunk text must not render",
          },
          {
            state: "source_missing",
            warning: "source_record_missing",
            count: 1,
          },
        ],
      },
      sync_sources: [],
      sync_runs: [],
      palace_runs: [],
    });

    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await page.getByText("Wake-up brief freshness").scrollIntoViewIfNeeded();
    await expect(page.getByText("Diary rollups covered")).toBeVisible();
    await expect(page.getByText("Tenant wake-up brief")).toBeVisible();
    await expect(page.getByText("Tenant shared")).toBeVisible();
    await expect(page.getByText("Wing 03-projects")).toBeVisible();
    await expect(page.getByText("2 diary rollups")).toBeVisible();
    await expect(page.getByText("1 diary rollup")).toBeVisible();
    await page.getByText("Wakeup trust health").scrollIntoViewIfNeeded();
    await expect(page.getByText("Source-backed")).toBeVisible();
    await expect(page.getByText("Generated", { exact: true })).toBeVisible();
    await expect(page.getByText("Stale / missing")).toBeVisible();
    await expect(page.getByText("Policy-limited")).toBeVisible();
    await expect(page.getByText("Latest checked")).toBeVisible();
    await expect(page.getByText("Latest 7 contexts classified")).toBeVisible();
    await expect(page.getByText("generated artifact without promoted source support")).toBeVisible();
    await expect(page.getByText("source record missing")).toBeVisible();
    await expect(page.getByText("raw chunk text must not render")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  });

  test("control tower can edit and delete a repo source", async ({ page }) => {
    const sourceId = "source-1";
    let currentTower = {
      tenant_id: "default",
      dirty_generation: 2,
      indexed_generation: 2,
      backlog_generation: 0,
      active_palace_run: null,
      memory_health: {
        queued: 0,
        processing: 0,
        failed: 0,
        retryable: 0,
        recent_jobs: [],
      },
      webhook_health: {
        configured: 0,
        pending: 0,
        terminal: 0,
        failed_jobs: 0,
        retryable_jobs: 0,
        recent_jobs: [],
      },
      fact_registry: {
        active: 0,
        superseded: 0,
        distinct_sources: 0,
        last_extracted_at: null,
        recent_facts: [],
      },
      diary_rollups: {
        fresh: 0,
        stale: 0,
        expected_through_day: "2026-04-22",
        last_refreshed_at: null,
        recent_rollups: [],
      },
      wakeup_briefs: {
        fresh: 0,
        stale: 0,
        generated_for_day: null,
        last_refreshed_at: null,
        recent_briefs: [],
      },
      sync_sources: [
        {
          id: sourceId,
          name: "Private repo",
          root_path: "https://github.com/palaceoftruth/palaceoftruth",
          source_kind: "repo",
          credential_type: "github_pat",
          has_stored_credential: true,
          status: "active",
          scan_interval_seconds: 900,
          allowed_extensions: [".md"],
          last_synced_at: null,
          last_error: null,
        },
      ],
      sync_runs: [],
      palace_runs: [],
    };
    let lastPatchBody: Record<string, unknown> | null = null;
    let deleteCalled = false;

    await page.route("**/api/v1/palace/control-tower", async (route) => {
      await route.fulfill({ json: currentTower });
    });
    await page.route("**/api/v1/palace/mcp-clients", async (route) => {
      await route.fulfill({
        json: {
          tenant_id: "default",
          clients: [],
          config_snippets: {
            codex_stdio_toml: "[mcp_servers.palaceoftruth-memory]",
            http_oauth_toml: "bearer_token_env_var = \"PALACEOFTRUTH_MCP_BEARER_TOKEN\"",
            oauth_token_command: "read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET",
            legacy_api_key_toml: "X-API-Key = \"set-from-your-secret-manager\"",
            secret_handling_note: "The client_secret is returned once.",
          },
        },
      });
    });
    await page.route(`**/api/v1/palace/sync-sources/${sourceId}`, async (route) => {
      if (route.request().method() === "PATCH") {
        lastPatchBody = route.request().postDataJSON() as Record<string, unknown>;
        currentTower = {
          ...currentTower,
          sync_sources: [
            {
              ...currentTower.sync_sources[0],
              name: String(lastPatchBody.name),
              credential_type: "deployment_github_pat",
              has_stored_credential: false,
              scan_interval_seconds: Number(lastPatchBody.scan_interval_seconds),
            },
          ],
        };
        await route.fulfill({ json: currentTower.sync_sources[0] });
        return;
      }

      deleteCalled = true;
      currentTower = { ...currentTower, sync_sources: [] };
      await route.fulfill({ json: { deleted: true, items_deactivated: 2 } });
    });

    await page.goto(`/palace/control-tower?e2e=${Date.now()}`);

    await expect(page.getByText("Private repo")).toBeVisible();
    await page.getByRole("button", { name: "Edit" }).click();
    await expect(page.getByText("Edit sync source")).toBeVisible();
    await expect(page.getByLabel("GitHub PAT")).toHaveValue("");

    await page.getByLabel("Sync source name").fill("Private repo (Hermes)");
    await page.getByLabel("Repo credential type").selectOption("deployment_github_pat");
    await page.getByRole("button", { name: "Save source" }).click();

    await expect.poll(() => lastPatchBody).not.toBeNull();
    expect(lastPatchBody).toMatchObject({
      name: "Private repo (Hermes)",
      credential_type: "deployment_github_pat",
      scan_interval_seconds: 900,
    });
    await expect(page.getByText("Private repo (Hermes)")).toBeVisible();
    await expect(page.getByText("deployment GitHub PAT")).toBeVisible();

    page.once("dialog", (dialog) => {
      expect(dialog.message()).toContain('Disable sync source "Private repo (Hermes)"');
      dialog.accept();
    });
    await page.getByRole("button", { name: "Disable" }).click();

    await expect.poll(() => deleteCalled).toBe(true);
    await expect(page.getByRole("heading", { name: /No sync sources/i })).toBeVisible();
  });

  test("keeps the selected wing and room during overview polling", async ({ page }) => {
    const homeWingId = "wing-home";
    const projectsWingId = "wing-projects";
    const homeRoomId = "room-home";
    const projectsRoomId = "room-launch";

    const overview = {
      tenant_id: "default",
      dirty_generation: 12,
      indexed_generation: 12,
      backlog_generation: 0,
      active_palace_run: null,
      latest_sync_runs: [],
      state_banner: null,
      wings: [
        {
          id: homeWingId,
          slug: "00-home",
          name: "00 Home",
          room_count: 1,
          item_count: 1,
          rooms: [
            {
              id: homeRoomId,
              wing_id: homeWingId,
              name: "Home Room",
              stable_key: "00-home:home-room",
              state: "active",
              item_count: 1,
              summary: "Home room summary.",
              membership_status: { status: "fresh", generation: 12, target_generation: 12, message: "fresh" },
              snapshot_status: { status: "fresh", generation: 12, target_generation: 12, message: "fresh" },
              tunnel_status: { status: "fresh", generation: 12, target_generation: 12, message: "fresh" },
              redirect_room_id: null,
            },
          ],
        },
        {
          id: projectsWingId,
          slug: "03-projects",
          name: "03 Projects",
          room_count: 1,
          item_count: 5,
          rooms: [
            {
              id: projectsRoomId,
              wing_id: projectsWingId,
              name: "Launch Plan",
              stable_key: "03-projects:launch-plan",
              state: "active",
              item_count: 5,
              summary: "Launch planning lives here.",
              membership_status: { status: "fresh", generation: 12, target_generation: 12, message: "fresh" },
              snapshot_status: { status: "fresh", generation: 12, target_generation: 12, message: "fresh" },
              tunnel_status: { status: "fresh", generation: 12, target_generation: 12, message: "fresh" },
              redirect_room_id: null,
            },
          ],
        },
      ],
    };

    const sources = [
      {
        id: "source-1",
        name: "Vault",
        root_path: "/mnt/palace-sync",
        source_kind: "folder",
        status: "active",
        scan_interval_seconds: 900,
        allowed_extensions: [".md"],
        last_synced_at: null,
        last_error: null,
      },
    ];

    const roomDetails = {
      [homeRoomId]: {
        room: overview.wings[0].rooms[0],
        wing_name: "00 Home",
        banner: null,
        representative_items: [
          {
            item_id: "item-home",
            title: "Home note",
            source_type: "note",
            summary: "Home note summary",
            membership_source: "auto",
            pinned: false,
          },
        ],
        tunnels: [],
        memberships: [
          {
            item_id: "item-home",
            title: "Home note",
            source_type: "note",
            summary: "Home note summary",
            membership_source: "auto",
            membership_kind: "primary",
            pinned: false,
          },
        ],
        redirect_target: null,
      },
      [projectsRoomId]: {
        room: overview.wings[1].rooms[0],
        wing_name: "03 Projects",
        banner: null,
        representative_items: [
          {
            item_id: "item-project",
            title: "Launch checklist",
            source_type: "note",
            summary: "Launch checklist summary",
            membership_source: "auto",
            pinned: false,
          },
        ],
        tunnels: [],
        memberships: [
          {
            item_id: "item-project",
            title: "Launch checklist",
            source_type: "note",
            summary: "Launch checklist summary",
            membership_source: "auto",
            membership_kind: "primary",
            pinned: false,
          },
        ],
        redirect_target: null,
      },
    };

    await mockPalaceControlTower(page, {
      tenant_id: "default",
      dirty_generation: 12,
      indexed_generation: 12,
      backlog_generation: 0,
      active_palace_run: null,
      sync_sources: sources,
      sync_runs: [],
      palace_runs: [],
    });
    await mockPalaceOverview(page, overview, sources);
    await page.route("**/api/v1/palace/rooms/*", async (route) => {
      const roomId = route.request().url().split("/").pop() ?? "";
      await route.fulfill({ json: roomDetails[roomId as keyof typeof roomDetails] });
    });

    await page.goto(`/palace?e2e=${Date.now()}`);

    const desktopNavigator = page.getByRole("navigation", { name: "Desktop Palace wing and room navigation" });
    await page.getByRole("button", { name: "03 Projects 1 rooms, 5 drawers" }).click();
    await expect(desktopNavigator.getByLabel("Rooms in 03 Projects")).toBeVisible();
    await page.getByRole("button", { name: /Launch Plan 5 drawers/ }).click();
    await expect(page.getByRole("heading", { name: "Launch Plan" })).toBeVisible();

    await page.waitForTimeout(6000);

    await expect(desktopNavigator.getByLabel("Rooms in 03 Projects")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Launch Plan" })).toBeVisible();
  });
});
