import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import {
  DEFAULT_WORKSPACE_LAYOUT,
  WORKSPACE_LAYOUT_LIMITS,
  fitWorkspaceLayout,
  readWorkspaceLayout,
  resetWorkspaceColumn,
  resizeWorkspaceColumn,
  setWorkspaceColumnWidth,
  showWorkspaceColumn,
  toggleWorkspaceColumn,
  workspaceColumnMaximum,
  writeWorkspaceLayout,
  type WorkspaceColumn,
  type WorkspaceLayoutState,
} from "../runtime/workspaceLayout";

function viewportWidth() {
  return typeof window === "undefined" ? Number.POSITIVE_INFINITY : window.innerWidth;
}

/** Owns pointer/keyboard resizing and local persistence for the desktop three-column shell. */
export function useWorkspaceLayout() {
  const [layout, setLayout] = useState<WorkspaceLayoutState>(() => readWorkspaceLayout(
    typeof window === "undefined" ? undefined : window.localStorage,
    viewportWidth(),
  ));
  const [resizingColumn, setResizingColumn] = useState<WorkspaceColumn | null>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    writeWorkspaceLayout(typeof window === "undefined" ? undefined : window.localStorage, layout);
  }, [layout]);

  useEffect(() => {
    function handleWindowResize() {
      setLayout((current) => fitWorkspaceLayout(current, viewportWidth()));
    }
    window.addEventListener("resize", handleWindowResize);
    return () => window.removeEventListener("resize", handleWindowResize);
  }, []);

  useEffect(() => () => dragCleanupRef.current?.(), []);

  const finishResize = useCallback(() => {
    dragCleanupRef.current?.();
  }, []);

  const beginResize = useCallback((column: WorkspaceColumn, event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragCleanupRef.current?.();
    const startX = event.clientX;
    const startLayout = layout;
    setResizingColumn(column);

    const handleMove = (moveEvent: globalThis.PointerEvent) => {
      setLayout(resizeWorkspaceColumn(startLayout, column, moveEvent.clientX - startX, viewportWidth()));
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", cleanup);
      window.removeEventListener("pointercancel", cleanup);
      window.removeEventListener("blur", cleanup);
      dragCleanupRef.current = null;
      setResizingColumn(null);
    };
    dragCleanupRef.current = cleanup;
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", cleanup);
    window.addEventListener("pointercancel", cleanup);
    window.addEventListener("blur", cleanup);
  }, [layout]);

  const toggleColumn = useCallback((column: WorkspaceColumn) => {
    finishResize();
    setLayout((current) => toggleWorkspaceColumn(current, column, viewportWidth()));
  }, [finishResize]);

  const resetColumn = useCallback((column: WorkspaceColumn) => {
    setLayout((current) => resetWorkspaceColumn(current, column, viewportWidth()));
  }, []);

  const showColumn = useCallback((column: WorkspaceColumn, preferredWidth?: number) => {
    finishResize();
    setLayout((current) => showWorkspaceColumn(current, column, viewportWidth(), preferredWidth));
  }, [finishResize]);

  const handleSeparatorKeyDown = useCallback((column: WorkspaceColumn, event: KeyboardEvent<HTMLDivElement>) => {
    const currentWidth = column === "left" ? layout.leftWidth : layout.rightWidth;
    let requestedWidth: number | null = null;
    if (event.key === "Home") {
      requestedWidth = column === "left" ? WORKSPACE_LAYOUT_LIMITS.leftMin : WORKSPACE_LAYOUT_LIMITS.rightMin;
    } else if (event.key === "End") {
      requestedWidth = column === "left" ? WORKSPACE_LAYOUT_LIMITS.leftMax : WORKSPACE_LAYOUT_LIMITS.rightMax;
    } else if (event.key === "ArrowLeft") {
      requestedWidth = currentWidth + (column === "left" ? -16 : 16);
    } else if (event.key === "ArrowRight") {
      requestedWidth = currentWidth + (column === "left" ? 16 : -16);
    }
    if (requestedWidth === null) return;
    event.preventDefault();
    setLayout((current) => setWorkspaceColumnWidth(current, column, requestedWidth!, viewportWidth()));
  }, [layout.leftWidth, layout.rightWidth]);

  return {
    layout,
    resizingColumn,
    beginResize,
    toggleColumn,
    showColumn,
    resetColumn,
    handleSeparatorKeyDown,
    maximumWidth: (column: WorkspaceColumn) => workspaceColumnMaximum(layout, column, viewportWidth()),
    defaults: DEFAULT_WORKSPACE_LAYOUT,
  };
}
