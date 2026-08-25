export type WorkspaceColumn = "left" | "right";

export interface WorkspaceLayoutState {
  leftWidth: number;
  rightWidth: number;
  leftCollapsed: boolean;
  rightCollapsed: boolean;
}

export interface WorkspaceLayoutStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const WORKSPACE_LAYOUT_STORAGE_KEY = "stellarcode.workspace-layout.v1";
export const DEFAULT_WORKSPACE_LAYOUT: WorkspaceLayoutState = {
  leftWidth: 236,
  rightWidth: 266,
  leftCollapsed: false,
  rightCollapsed: false,
};

export const WORKSPACE_LAYOUT_LIMITS = {
  leftMin: 188,
  leftMax: 420,
  rightMin: 220,
  // The right workbench may grow to the viewport boundary. The effective
  // maximum is still constrained by centerMin in setWorkspaceColumnWidth().
  rightMax: 10_000,
  centerMin: 320,
  dividerWidth: 5,
} as const;

/** Loads only validated layout preferences; corrupt local data falls back safely. */
export function readWorkspaceLayout(
  storage?: WorkspaceLayoutStorage,
  viewportWidth = Number.POSITIVE_INFINITY,
): WorkspaceLayoutState {
  if (!storage) return fitWorkspaceLayout(DEFAULT_WORKSPACE_LAYOUT, viewportWidth);
  try {
    const value = storage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY);
    if (!value) return fitWorkspaceLayout(DEFAULT_WORKSPACE_LAYOUT, viewportWidth);
    const parsed = JSON.parse(value) as Partial<WorkspaceLayoutState>;
    return fitWorkspaceLayout({
      leftWidth: finiteNumber(parsed.leftWidth, DEFAULT_WORKSPACE_LAYOUT.leftWidth),
      rightWidth: finiteNumber(parsed.rightWidth, DEFAULT_WORKSPACE_LAYOUT.rightWidth),
      leftCollapsed: parsed.leftCollapsed === true,
      rightCollapsed: parsed.rightCollapsed === true,
    }, viewportWidth);
  } catch {
    return fitWorkspaceLayout(DEFAULT_WORKSPACE_LAYOUT, viewportWidth);
  }
}

export function writeWorkspaceLayout(storage: WorkspaceLayoutStorage | undefined, state: WorkspaceLayoutState) {
  if (!storage) return;
  try {
    storage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Layout persistence must never make the desktop shell unusable.
  }
}

/** Keeps both visible sidebars inside the viewport while preserving the center workspace. */
export function fitWorkspaceLayout(state: WorkspaceLayoutState, viewportWidth: number): WorkspaceLayoutState {
  let leftWidth = clamp(state.leftWidth, WORKSPACE_LAYOUT_LIMITS.leftMin, WORKSPACE_LAYOUT_LIMITS.leftMax);
  let rightWidth = clamp(state.rightWidth, WORKSPACE_LAYOUT_LIMITS.rightMin, WORKSPACE_LAYOUT_LIMITS.rightMax);
  if (!Number.isFinite(viewportWidth)) return { ...state, leftWidth, rightWidth };

  const dividerCount = Number(!state.leftCollapsed) + Number(!state.rightCollapsed);
  const sidebarBudget = Math.max(
    0,
    viewportWidth - WORKSPACE_LAYOUT_LIMITS.centerMin - dividerCount * WORKSPACE_LAYOUT_LIMITS.dividerWidth,
  );
  let overflow = (state.leftCollapsed ? 0 : leftWidth)
    + (state.rightCollapsed ? 0 : rightWidth)
    - sidebarBudget;

  if (overflow > 0 && !state.rightCollapsed) {
    const reduction = Math.min(overflow, rightWidth - WORKSPACE_LAYOUT_LIMITS.rightMin);
    rightWidth -= reduction;
    overflow -= reduction;
  }
  if (overflow > 0 && !state.leftCollapsed) {
    const reduction = Math.min(overflow, leftWidth - WORKSPACE_LAYOUT_LIMITS.leftMin);
    leftWidth -= reduction;
  }
  return { ...state, leftWidth: Math.round(leftWidth), rightWidth: Math.round(rightWidth) };
}

export function resizeWorkspaceColumn(
  state: WorkspaceLayoutState,
  column: WorkspaceColumn,
  pointerDelta: number,
  viewportWidth: number,
): WorkspaceLayoutState {
  const requested = column === "left"
    ? state.leftWidth + pointerDelta
    : state.rightWidth - pointerDelta;
  return setWorkspaceColumnWidth(state, column, requested, viewportWidth);
}

export function setWorkspaceColumnWidth(
  state: WorkspaceLayoutState,
  column: WorkspaceColumn,
  requestedWidth: number,
  viewportWidth: number,
): WorkspaceLayoutState {
  const minimum = column === "left" ? WORKSPACE_LAYOUT_LIMITS.leftMin : WORKSPACE_LAYOUT_LIMITS.rightMin;
  const maximum = workspaceColumnMaximum(state, column, viewportWidth);
  const width = Math.round(clamp(requestedWidth, minimum, maximum));
  return column === "left" ? { ...state, leftWidth: width } : { ...state, rightWidth: width };
}

export function workspaceColumnMaximum(
  state: WorkspaceLayoutState,
  column: WorkspaceColumn,
  viewportWidth: number,
) {
  const minimum = column === "left" ? WORKSPACE_LAYOUT_LIMITS.leftMin : WORKSPACE_LAYOUT_LIMITS.rightMin;
  const fixedMaximum = column === "left" ? WORKSPACE_LAYOUT_LIMITS.leftMax : WORKSPACE_LAYOUT_LIMITS.rightMax;
  const otherVisibleWidth = column === "left"
    ? state.rightCollapsed ? 0 : state.rightWidth
    : state.leftCollapsed ? 0 : state.leftWidth;
  const dividerCount = Number(!state.leftCollapsed) + Number(!state.rightCollapsed);
  const viewportMaximum = Number.isFinite(viewportWidth)
    ? viewportWidth - otherVisibleWidth - WORKSPACE_LAYOUT_LIMITS.centerMin
      - dividerCount * WORKSPACE_LAYOUT_LIMITS.dividerWidth
    : fixedMaximum;
  return Math.round(Math.max(minimum, Math.min(fixedMaximum, viewportMaximum)));
}

export function toggleWorkspaceColumn(
  state: WorkspaceLayoutState,
  column: WorkspaceColumn,
  viewportWidth: number,
): WorkspaceLayoutState {
  const next = column === "left"
    ? { ...state, leftCollapsed: !state.leftCollapsed }
    : { ...state, rightCollapsed: !state.rightCollapsed };
  return fitWorkspaceLayout(next, viewportWidth);
}

export function showWorkspaceColumn(
  state: WorkspaceLayoutState,
  column: WorkspaceColumn,
  viewportWidth: number,
  preferredWidth?: number,
): WorkspaceLayoutState {
  const next = column === "left"
    ? {
        ...state,
        leftCollapsed: false,
        leftWidth: preferredWidth === undefined ? state.leftWidth : Math.max(state.leftWidth, preferredWidth),
      }
    : {
        ...state,
        rightCollapsed: false,
        rightWidth: preferredWidth === undefined ? state.rightWidth : Math.max(state.rightWidth, preferredWidth),
      };
  return fitWorkspaceLayout(next, viewportWidth);
}

export function resetWorkspaceColumn(
  state: WorkspaceLayoutState,
  column: WorkspaceColumn,
  viewportWidth: number,
) {
  const next = column === "left"
    ? { ...state, leftWidth: DEFAULT_WORKSPACE_LAYOUT.leftWidth }
    : { ...state, rightWidth: DEFAULT_WORKSPACE_LAYOUT.rightWidth };
  return fitWorkspaceLayout(next, viewportWidth);
}

function finiteNumber(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value));
}
