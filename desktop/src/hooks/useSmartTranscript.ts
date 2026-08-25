import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
  type UIEventHandler,
} from "react";

const DEFAULT_FOLLOW_THRESHOLD_PX = 80;

export interface UseSmartTranscriptOptions {
  /**
   * A stable value that changes only when new transcript output is appended.
   * Call `notifyContentChanged` instead when the producer already has an event
   * callback. Do not use both mechanisms for the same update.
   */
  contentVersion?: unknown;
  /** Distance from the bottom that is still considered "following". */
  followThreshold?: number;
  /** Whether a newly mounted transcript should start at the bottom. */
  initiallyFollowing?: boolean;
}

export interface UseSmartTranscriptResult<T extends HTMLElement> {
  containerRef: RefObject<T | null>;
  isFollowingBottom: boolean;
  unreadOutputCount: number;
  onScroll: UIEventHandler<T>;
  /** Notify the hook that output was appended; defaults to one unread item. */
  notifyContentChanged: (unreadIncrement?: number) => void;
  jumpToBottom: (behavior?: ScrollBehavior) => void;
}

function normalizeUnreadIncrement(value: number): number {
  if (!Number.isFinite(value) || value <= 0) {
    return 0;
  }
  return Math.max(1, Math.trunc(value));
}

/**
 * Keeps a streaming transcript pinned to the bottom until the user scrolls up.
 * New output never steals the scroll position from a user reading older content.
 */
export function useSmartTranscript<T extends HTMLElement = HTMLDivElement>(
  options: UseSmartTranscriptOptions = {},
): UseSmartTranscriptResult<T> {
  const {
    contentVersion,
    followThreshold = DEFAULT_FOLLOW_THRESHOLD_PX,
    initiallyFollowing = true,
  } = options;

  const containerRef = useRef<T | null>(null);
  const [isFollowingBottom, setIsFollowingBottom] = useState(initiallyFollowing);
  const [unreadOutputCount, setUnreadOutputCount] = useState(0);

  const followingRef = useRef(initiallyFollowing);
  const animationFrameRef = useRef<number | null>(null);
  const pendingUnreadIncrementRef = useRef(0);
  const hasObservedContentVersionRef = useRef(false);
  const previousContentVersionRef = useRef<unknown>(contentVersion);

  const setFollowing = useCallback((following: boolean) => {
    followingRef.current = following;
    setIsFollowingBottom(following);
    if (following) {
      setUnreadOutputCount(0);
    }
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    container.scrollTo({ top: container.scrollHeight, behavior });
  }, []);

  const jumpToBottom = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      setFollowing(true);
      scrollToBottom(behavior);
    },
    [scrollToBottom, setFollowing],
  );

  const flushPendingContentChange = useCallback(() => {
    animationFrameRef.current = null;
    const unreadIncrement = pendingUnreadIncrementRef.current;
    pendingUnreadIncrementRef.current = 0;

    if (followingRef.current) {
      scrollToBottom("auto");
      return;
    }

    if (unreadIncrement > 0) {
      setUnreadOutputCount((current) => current + unreadIncrement);
    }
  }, [scrollToBottom]);

  const notifyContentChanged = useCallback(
    (unreadIncrement = 1) => {
      pendingUnreadIncrementRef.current += normalizeUnreadIncrement(unreadIncrement);
      if (animationFrameRef.current !== null) {
        return;
      }
      animationFrameRef.current = window.requestAnimationFrame(flushPendingContentChange);
    },
    [flushPendingContentChange],
  );

  const onScroll = useCallback<UIEventHandler<T>>(
    (event) => {
      const container = event.currentTarget;
      const distanceFromBottom =
        container.scrollHeight - container.clientHeight - container.scrollTop;
      const threshold = Math.max(0, followThreshold);
      setFollowing(distanceFromBottom <= threshold);
    },
    [followThreshold, setFollowing],
  );

  useLayoutEffect(() => {
    if (!hasObservedContentVersionRef.current) {
      hasObservedContentVersionRef.current = true;
      previousContentVersionRef.current = contentVersion;
      if (followingRef.current) {
        scrollToBottom("auto");
      }
      return;
    }

    if (Object.is(previousContentVersionRef.current, contentVersion)) {
      return;
    }
    previousContentVersionRef.current = contentVersion;
    notifyContentChanged();
  }, [contentVersion, notifyContentChanged, scrollToBottom]);

  useEffect(
    () => () => {
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
      }
    },
    [],
  );

  return {
    containerRef,
    isFollowingBottom,
    unreadOutputCount,
    onScroll,
    notifyContentChanged,
    jumpToBottom,
  };
}
