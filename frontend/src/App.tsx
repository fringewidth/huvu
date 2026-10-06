import { lazy, Suspense, useEffect } from "react";
import { LazyMotion, MotionConfig } from "motion/react";
import { boot, useSession } from "./account/session";
import { GardenCanvas } from "./components/GardenCanvas";
import { Stage } from "./components/Stage";
import { SHARED } from "./config/interaction";

// Motion's animation code loads after the first paint instead of before it.
const motionFeatures = () => import("./motionFeatures").then((mod) => mod.default);
// A device logs in or pairs once, so those screens load only when needed.
const Login = lazy(() => import("./components/Login").then((mod) => ({ default: mod.Login })));
const Pairing = lazy(() => import("./components/Pairing").then((mod) => ({ default: mod.Pairing })));

function App() {
  const { status } = useSession();

  useEffect(() => {
    void boot();
  }, []);

  // A device with no token logs in, or pairs if it is a shared one. While
  // the token is read (a moment), only the garden shows.
  const front = { booting: null, out: SHARED ? <Pairing /> : <Login />, in: <Stage /> }[status];

  return (
    <>
      {/* Stays mounted underneath at all times, in every mode and view, so
          the garland never remounts. */}
      <GardenCanvas />
      {/* With the system's reduce-motion setting on, transitions keep their
          fades but drop movement and scaling. */}
      <LazyMotion features={motionFeatures} strict>
        <MotionConfig reducedMotion="user">
          <Suspense fallback={null}>{front}</Suspense>
        </MotionConfig>
      </LazyMotion>
    </>
  );
}

export default App;
