// huvu's Email Worker: Cloudflare Email Routing hands it the mail sent to
// one of huvu's addresses, and it posts each mail as it came to huvu's mail
// source (backend/src/huvu_drivers/mail.py), which keeps it for the
// address's person. It reads nothing and keeps nothing itself.
//
// HUVU_URL is the source's ingest address and HUVU_TOKEN the person's
// tracker token, both printed by `huvu tracker add <user> --mail`.
// FORWARD_TO, when set, is the person's own inbox: every mail goes there
// too, unchanged, so they read it as before. It must be a destination
// address verified in Email Routing. Never set it to an inbox that
// forwards to this address, or each mail goes round for good.

export default {
  async email(message, env) {
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    let kept = false;
    if (env.FORWARD_TO) {
      try {
        await message.forward(env.FORWARD_TO);
        kept = true;
      } catch (err) {
        console.error(`couldn't forward to the inbox: ${err}`);
      }
    }
    let why;
    try {
      const reply = await fetch(env.HUVU_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.HUVU_TOKEN}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ raw: base64(raw), from: message.from }),
      });
      why = reply.ok ? null : `huvu refused the mail: ${reply.status} ${await reply.text()}`;
    } catch (err) {
      why = `huvu is unreachable: ${err}`;
    }
    if (!why) {
      return;
    }
    console.error(why);
    // With a copy in the person's inbox, nothing is lost: they can forward
    // it here again once huvu is back. With none, the sender gets a bounce.
    if (!kept) {
      message.setReject(why);
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
