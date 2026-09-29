import { createServer } from "node:http";
import wrtc from "@roamhq/wrtc";

const { RTCPeerConnection } = wrtc;
const { RTCVideoSource } = wrtc.nonstandard;

const PORT = 3001;

function sendCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  // WHEP clients read their session's URL from Location.
  res.setHeader("Access-Control-Expose-Headers", "Location");
}

const server = createServer(async (req, res) => {
  sendCors(res);

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method === "POST" && req.url === FEED_PATH) {
    await answerFeed(req, res);
    return;
  }

  if (req.method === "DELETE" && req.url.startsWith(`${FEED_PATH}/`)) {
    sessions.get(req.url.slice(FEED_PATH.length + 1))?.();
    res.writeHead(200);
    res.end();
    return;
  }

  if (req.method !== "POST" || req.url !== "/api/chat") {
    res.writeHead(404);
    res.end();
    return;
  }

  res.writeHead(200, {
    "Content-Type": "text/plain; charset=utf-8",
    "Transfer-Encoding": "chunked",
  });

  const text =
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Proin sed euismod erat, eget vulputate leo. Praesent lacus ante, dapibus eu ligula eu, fringilla varius ex. Nullam vel tincidunt lectus. Quisque eget erat nisi. Nunc vitae lorem vehicula ligula convallis lobortis sed nec arcu. Suspendisse potenti. Quisque ornare hendrerit diam, eget sagittis metus faucibus a. In ultricies lacus pulvinar massa gravida imperdiet. Etiam sit amet orci vitae enim sodales pulvinar. Mauris bibendum lectus ligula.";
  for (const word of text.split(" ")) {
    res.write(word + " ");
    await new Promise((r) => setTimeout(r, 60));
  }

  res.end();
});

// A live video feed, as the real gateway will send one: WebRTC, signaled
// over WHEP. The client POSTs its SDP offer, gets the answer back with a
// Location for its session, and DELETEs that Location to hang up. The
// picture is a test pattern, an ivory bar sweeping across the paper color,
// drawn as raw frames that wrtc encodes to VP8 itself.
const FEED_W = 640;
const FEED_H = 360;
const FEED_FPS = 30;
const FEED_PATH = "/api/feed/demo";
// Waiting for every ICE candidate lets the answer carry them all, so the
// client needs no trickle ICE. On a server this finishes in milliseconds.
const GATHER_MS = 2000;

const sessions = new Map();
let nextSession = 0;

async function answerFeed(req, res) {
  let offer = "";
  for await (const chunk of req) offer += chunk;

  const pc = new RTCPeerConnection();
  const source = new RTCVideoSource();
  pc.addTrack(source.createTrack());
  let n = 0;
  const timer = setInterval(() => source.onFrame(testFrame(n++)), 1000 / FEED_FPS);
  const id = String(++nextSession);
  const close = () => {
    if (!sessions.delete(id)) return;
    clearInterval(timer);
    pc.close();
  };
  sessions.set(id, close);
  // A client that vanishes without a DELETE ends here once ICE gives up.
  pc.oniceconnectionstatechange = () => {
    if (pc.iceConnectionState === "failed" || pc.iceConnectionState === "closed") close();
  };

  try {
    await pc.setRemoteDescription({ type: "offer", sdp: offer });
    await pc.setLocalDescription(await pc.createAnswer());
    await gathered(pc);
  } catch (err) {
    console.error("feed: negotiation failed", err);
    close();
    res.writeHead(400);
    res.end();
    return;
  }
  res.writeHead(201, {
    "Content-Type": "application/sdp",
    Location: `${FEED_PATH}/${id}`,
  });
  res.end(pc.localDescription.sdp);
}

function gathered(pc) {
  return new Promise((resolve) => {
    const timeout = setTimeout(resolve, GATHER_MS);
    pc.onicecandidate = (e) => {
      if (e.candidate) return;
      clearTimeout(timeout);
      resolve();
    };
  });
}

// The paper and ivory from the frontend palette, in YUV.
const PAPER_YUV = [29, 132, 127];
const IVORY_YUV = [216, 120, 133];
const BAR_W = 80;

// One I420 frame: a full-size Y plane, then U and V at half size each way.
function testFrame(n) {
  const data = new Uint8ClampedArray(FEED_W * FEED_H * 1.5);
  const barX = ((n * 6) % (FEED_W + BAR_W)) - BAR_W;
  const planes = [
    { offset: 0, w: FEED_W, h: FEED_H, scale: 1 },
    { offset: FEED_W * FEED_H, w: FEED_W / 2, h: FEED_H / 2, scale: 2 },
    { offset: FEED_W * FEED_H * 1.25, w: FEED_W / 2, h: FEED_H / 2, scale: 2 },
  ];
  planes.forEach(({ offset, w, h, scale }, p) => {
    data.fill(PAPER_YUV[p], offset, offset + w * h);
    const from = Math.max(0, Math.floor(barX / scale));
    const to = Math.min(w, Math.floor((barX + BAR_W) / scale));
    if (to <= from) return;
    for (let y = 0; y < h; y++) data.fill(IVORY_YUV[p], offset + y * w + from, offset + y * w + to);
  });
  return { width: FEED_W, height: FEED_H, data };
}

server.listen(PORT, () => {
  console.log(`mock chat server listening on http://localhost:${PORT}`);
});
