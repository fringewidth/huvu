import { useEffect, useState } from "react";

// Plays a live WebRTC feed from a WHEP endpoint: POST our SDP offer to
// `src`, get the answer and the Location of our session back, and DELETE
// that Location to hang up. Any WHEP server works, from the huvu gateway
// to a media server relaying a camera.
export type FeedState = "connecting" | "live" | "lost";

// The answer only needs our candidates if the server can't reach us
// first. A public gateway can, so this waits for all of them, but never
// long: whatever has been gathered by then goes in the offer.
const GATHER_MS = 1500;

export function useWhepFeed(src: string): { stream: MediaStream | null; state: FeedState } {
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [state, setState] = useState<FeedState>("connecting");

  useEffect(() => {
    if (!src) return;
    const pc = new RTCPeerConnection();
    const abort = new AbortController();
    let session: string | null = null;
    const hangUp = () => {
      if (session) void fetch(session, { method: "DELETE" }).catch(() => {});
      session = null;
    };

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = (e) => setStream(e.streams[0] ?? new MediaStream([e.track]));
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "connected") setState("live");
      if (pc.connectionState === "failed") setState("lost");
    };

    void (async () => {
      try {
        await pc.setLocalDescription(await pc.createOffer());
        await gathered(pc);
        const res = await fetch(src, {
          method: "POST",
          headers: { "Content-Type": "application/sdp" },
          body: pc.localDescription?.sdp,
          signal: abort.signal,
        });
        if (res.status !== 201) throw new Error(`feed: WHEP answered ${res.status}`);
        const location = res.headers.get("Location");
        session = location && new URL(location, src).href;
        // Unmounted while the answer was on its way.
        if (abort.signal.aborted) return hangUp();
        await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
      } catch (err) {
        if (abort.signal.aborted) return;
        console.error("feed: could not connect", err);
        setState("lost");
      }
    })();

    return () => {
      abort.abort();
      pc.close();
      hangUp();
    };
  }, [src]);

  return { stream, state };
}

function gathered(pc: RTCPeerConnection): Promise<void> {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const timeout = setTimeout(resolve, GATHER_MS);
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState !== "complete") return;
      clearTimeout(timeout);
      resolve();
    });
  });
}
