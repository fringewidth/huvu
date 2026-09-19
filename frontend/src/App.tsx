import { AssistantRuntimeProvider, useLocalRuntime } from "@assistant-ui/react";
import { Chat } from "./components/Chat";
import { mockChatAdapter } from "./chatRuntime";

function App() {
  const runtime = useLocalRuntime(mockChatAdapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Chat />
    </AssistantRuntimeProvider>
  );
}

export default App;
