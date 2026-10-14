// huvu's Email Worker: Cloudflare Email Routing hands it the mail sent to
// one of huvu's addresses, and it posts each mail as it came to huvu's mail
// source (backend/src/huvu_drivers/mail.py), which keeps it for the
// address's person. It reads nothing and keeps nothing itself.
//
// HUVU_URL is the source's ingest address and HUVU_TOKEN the person's
// tracker token, both printed by `huvu tracker add <user> --mail`.

export default {
  async email(message, env) {
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    let reply;
    try {
      reply = await fetch(env.HUVU_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.HUVU_TOKEN}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ raw: base64(raw), from: message.from }),
      });
    } catch (err) {
      // The mail stays in the inbox it was forwarded from, so a bounce
      // loses nothing; the person forwards it again once huvu is back.
      message.setReject(`huvu is unreachable: ${err}`);
      return;
    }
    if (!reply.ok) {
      message.setReject(`huvu refused the mail: ${reply.status} ${await reply.text()}`);
    }
  },
};

// btoa takes a string of bytes, built a slice at a time, since a 25 MiB
// mail is too many arguments for one String.fromCharCode call.
function base64(bytes) {
  const slices = [];
  for (let i = 0; i < bytes.length; i += 0x8000) {
    slices.push(String.fromCharCode(...bytes.subarray(i, i + 0x8000)));
  }
  return btoa(slices.join(""));
}
