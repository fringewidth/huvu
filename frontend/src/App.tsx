import { useState } from "react";
import { AssistantRuntimeProvider, useLocalRuntime } from "@assistant-ui/react";
import { Chat } from "./components/Chat";
import { VoiceOrb } from "./components/VoiceOrb";
import { ModeToggle } from "./components/ModeToggle";
import { mockChatAdapter } from "./chatRuntime";
import { initialMode, modeSwitchable, TARGET, type Mode } from "./config/interaction";

function App() {
  const runtime = useLocalRuntime(mockChatAdapter);
  const [mode, setMode] = useState<Mode>(initialMode);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {modeSwitchable[TARGET] && <ModeToggle mode={mode} onChange={setMode} />}
      {mode === "text" ? <Chat /> : <VoiceOrb />}
    </AssistantRuntimeProvider>
  );
}

export default App;
