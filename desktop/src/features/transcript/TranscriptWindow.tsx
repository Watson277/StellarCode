import { Fragment, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import type { Translator } from "../../i18n";

const PAGE_SIZE = 80;

/** Mount recent history first; older messages remain available without deleting state. */
export function TranscriptWindow<T extends { id: string }>({ items, containerRef, t, renderItem }: {
  items: T[];
  containerRef: RefObject<HTMLDivElement | null>;
  t: Translator;
  renderItem: (item: T) => ReactNode;
}) {
  const [anchor, setAnchor] = useState<string | null>(null);
  const previousScroll = useRef<{ height: number; top: number } | null>(null);
  const anchorIndex = anchor === null ? -1 : items.findIndex((item) => item.id === anchor);
  const start = anchorIndex < 0 ? Math.max(0, items.length - PAGE_SIZE) : anchorIndex;

  useLayoutEffect(() => {
    if (items.length && anchorIndex < 0) setAnchor(items[start].id);
    const container = containerRef.current;
    if (container && previousScroll.current) {
      const previous = previousScroll.current;
      container.scrollTop = previous.top + container.scrollHeight - previous.height;
      previousScroll.current = null;
    }
  }, [anchorIndex, start, items, containerRef]);

  function showEarlier() {
    const container = containerRef.current;
    if (container) previousScroll.current = { height: container.scrollHeight, top: container.scrollTop };
    setAnchor(items[Math.max(0, start - PAGE_SIZE)].id);
  }

  return <>
    {start > 0 && <div className="transcript-history-control"><button type="button" className="secondary-button" onClick={showEarlier}>
      {t("Show earlier messages ({count})", { count: start })}
    </button></div>}
    {items.slice(start).map((item) => <Fragment key={item.id}>{renderItem(item)}</Fragment>)}
  </>;
}
