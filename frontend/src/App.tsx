import { LazyMotion, MotionConfig } from "motion/react";
import { GardenCanvas } from "./components/GardenCanvas";
import { Stage } from "./components/Stage";

// Motion's animation code loads after the first paint instead of before it.
const motionFeatures = () => import("./motionFeatures").then((mod) => mod.default);

function App() {
  return (
    <>
      {/* Stays mounted underneath at all times, in every mode and view, so
          the garland never remounts. */}
      <GardenCanvas />
      {/* With the system's reduce-motion setting on, transitions keep their
          fades but drop movement and scaling. */}
      <LazyMotion features={motionFeatures} strict>
        <MotionConfig reducedMotion="user">
          <Stage />
        </MotionConfig>
      </LazyMotion>
    </>
  );
}

export default App;
