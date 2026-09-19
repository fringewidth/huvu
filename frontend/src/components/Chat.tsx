import { ComposerPrimitive, ThreadPrimitive } from "@assistant-ui/react";
import { Message } from "./Message";
import "../styles/Chat.css";

export function Chat() {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="viewport">
        <ThreadPrimitive.Messages components={{ Message }} />
      </ThreadPrimitive.Viewport>
      <ComposerPrimitive.Root className="composer">
        <ComposerPrimitive.Input
          className="composer-input"
          placeholder="Say something to huvu..."
        />
        <ComposerPrimitive.Send className="composer-send">
          Send
        </ComposerPrimitive.Send>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}
