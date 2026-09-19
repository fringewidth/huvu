import { createServer } from "node:http";

const PORT = 3001;

function sendCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}

const server = createServer(async (req, res) => {
  sendCors(res);

  if (req.method === "OPTIONS") {
    res.writeHead(204);
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

  const words = ["huvu", "huvu", "huvu"];
  for (const word of words) {
    res.write(word + " ");
    await new Promise((r) => setTimeout(r, 200));
  }

  res.end();
});

server.listen(PORT, () => {
  console.log(`mock chat server listening on http://localhost:${PORT}`);
});
