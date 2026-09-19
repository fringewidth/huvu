import { MessagePrimitive } from "@assistant-ui/react";
import "../styles/Message.css";

export function Message() {
  return (
    <MessagePrimitive.Root className="message">
      <MessagePrimitive.Content />
    </MessagePrimitive.Root>
  );
}
