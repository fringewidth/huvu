import { MessagePrimitive } from "@assistant-ui/react";

// User turns: a plain, utilitarian glass bubble — sans-serif,
// right-aligned, capped width, same as most chat UIs — but frosted so the
// garden canvas still shows through faintly.
export function UserMessage() {
  return (
    <MessagePrimitive.Root className="self-end max-w-[85%] rounded-xl border border-border/50 bg-card/60 px-4 py-3 font-sans text-[0.95rem] text-foreground shadow-lg backdrop-blur-md whitespace-pre-wrap">
      <MessagePrimitive.Content />
    </MessagePrimitive.Root>
  );
}

// Assistant turns: no bubble, full width — this is huvu talking, given
// the serif voice rather than boxed in like a plain UI element.
export function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="huvu-text self-stretch max-w-full [font-family:var(--serif)] text-[1.05rem] leading-[1.7] text-foreground whitespace-pre-wrap">
      <MessagePrimitive.Content />
    </MessagePrimitive.Root>
  );
}
