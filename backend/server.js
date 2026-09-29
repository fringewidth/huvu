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

  const text =
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Proin sed euismod erat, eget vulputate leo. Praesent lacus ante, dapibus eu ligula eu, fringilla varius ex. Nullam vel tincidunt lectus. Quisque eget erat nisi. Nunc vitae lorem vehicula ligula convallis lobortis sed nec arcu. Suspendisse potenti. Quisque ornare hendrerit diam, eget sagittis metus faucibus a. In ultricies lacus pulvinar massa gravida imperdiet. Etiam sit amet orci vitae enim sodales pulvinar. Mauris bibendum lectus ligula.";
  for (const word of text.split(" ")) {
    res.write(word + " ");
    await new Promise((r) => setTimeout(r, 60));
  }

  res.end();
});

server.listen(PORT, () => {
  console.log(`mock chat server listening on http://localhost:${PORT}`);
});
