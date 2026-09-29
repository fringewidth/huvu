import { ComposerPrimitive, ThreadPrimitive } from "@assistant-ui/react";
import { AssistantMessage, UserMessage } from "./Message";
import { Button } from "./ui/button";

export function Chat() {
  return (
    <ThreadPrimitive.Root className="relative z-10 mx-auto flex h-screen w-full max-w-[46rem] flex-col text-left font-sans text-foreground">
      <ThreadPrimitive.Viewport className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-8 sm:px-6 [justify-content:flex-end]">
        <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
      </ThreadPrimitive.Viewport>
      <ComposerPrimitive.Root className="flex items-end gap-2.5 border-t border-border/60 bg-background/30 px-4 py-5 backdrop-blur-xl sm:px-6">
        <ComposerPrimitive.Input
          className="flex-1 resize-none rounded-xl border border-border bg-card/50 px-4 py-3 text-foreground placeholder:text-muted-foreground backdrop-blur-md focus:ring-2 focus:ring-ring/50 focus:outline-none"
          placeholder="Say something to huvu..."
        />
        <ComposerPrimitive.Send asChild>
          <Button
            variant="outline"
            className="rounded-full border-[var(--accent-border)] bg-card/40 text-[var(--accent)] backdrop-blur-md hover:bg-[var(--accent-bg)]"
          >
            Send
          </Button>
        </ComposerPrimitive.Send>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}
