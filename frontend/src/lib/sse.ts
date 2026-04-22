/**
 * Minimal wrapper around the browser's `EventSource` so tests can
 * inject a fake.
 *
 * The backend emits one event per node status transition; the kind
 * (``node_status``, ``node_usage``, ...) is the SSE ``event`` field
 * and the body is the JSON-encoded ``WorkFlowEvent`` dump.
 */

import type { WorkFlowEvent } from "@/types/schema";

export interface SSESubscription {
  close: () => void;
}

export type SSEFactory = (url: string) => EventSource;

const defaultFactory: SSEFactory = (url) => new EventSource(url);

export function subscribeEvents(
  url: string,
  handlers: {
    onEvent: (event: WorkFlowEvent) => void;
    onError?: (err: Event) => void;
    /** Fires on initial connect AND every auto-reconnect. The backend
     * doesn't tag events with ``id:`` so EventSource can't replay
     * missed events via Last-Event-ID; callers should use this hook
     * to re-fetch full state, otherwise events emitted during a
     * disconnect window are lost forever (e.g. a 2.5-min turn whose
     * ``chat.turn.completed`` lands while the proxy idle-timed the
     * connection — UI shows status=succeeded but no agent_response). */
    onOpen?: () => void;
  },
  factory: SSEFactory = defaultFactory,
): SSESubscription {
  const source = factory(url);

  const onMessage = (msg: MessageEvent<string>) => {
    try {
      const parsed = JSON.parse(msg.data) as WorkFlowEvent;
      handlers.onEvent(parsed);
    } catch (err) {
      // Malformed payload — skip. The backend always emits valid
      // JSON so this should only happen if something is very wrong;
      // swallowing it here keeps the stream alive for the next event.
      // eslint-disable-next-line no-console
      console.warn("dropped malformed SSE payload", err);
    }
  };

  // The backend uses named events, not the unnamed ``message`` channel,
  // so we subscribe to the known kinds plus fall back to ``message``
  // for future additions.
  const kinds = [
    "chat.node.created",
    "chat.node.status",
    "chat.node.queue.updated",
    "chat.node.deleted",
    "chat.deleted",
    "chat.turn.started",
    "chat.turn.completed",
    "chat.workflow.node.running",
    "chat.workflow.node.succeeded",
    "chat.workflow.node.failed",
    "chat.workflow.node.token",
    "message",
  ];
  for (const kind of kinds) {
    source.addEventListener(kind, onMessage as EventListener);
  }

  if (handlers.onError) {
    source.addEventListener("error", handlers.onError);
  }
  if (handlers.onOpen) {
    source.addEventListener("open", handlers.onOpen);
  }

  return {
    close: () => source.close(),
  };
}
