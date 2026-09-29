import { AssistantRuntimeProvider, useLocalRuntime } from "@assistant-ui/react";
import { MotionConfig } from "motion/react";
import { GardenCanvas } from "./components/GardenCanvas";
import { Stage } from "./components/Stage";
import { mockChatAdapter } from "./chatRuntime";
import { devSeed } from "./devSeed";

const SEED = devSeed();

function App() {
  const runtime = useLocalRuntime(mockChatAdapter, { initialMessages: SEED });

  return (
    // Looks unused, since Stage talks to the runtime directly, but it is
    // what mounts the runtime's thread: without it the thread never loads
    // its initial messages.
    <AssistantRuntimeProvider runtime={runtime}>
      {/* Stays mounted underneath at all times, in every mode and view, so
          the garland never remounts. */}
      <GardenCanvas />
      {/* With the system's reduce-motion setting on, transitions keep their
          fades but drop movement and scaling. */}
      <MotionConfig reducedMotion="user">
        <Stage runtime={runtime} />
      </MotionConfig>
    </AssistantRuntimeProvider>
  );
}

export default App;
