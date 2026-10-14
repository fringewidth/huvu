# Sanity check before the family uses huvu

Go through this list on real devices before you hand huvu to anyone. Each check says what to do and what you should see. When you see something else, note it down and move on.

The last section lists what doesn't work yet, so you don't spend time on it.

## What you need

1. The backend running, with Hermes behind it.
1. Two family members added (`./setup.sh <username> <password> [name]`), so you can test one person asking and another answering.
1. Two Android phones with the app installed, one logged in as each person. A second phone matters for approvals and for "is it you?".
1. A laptop with the web app open.
1. The Fire TV, for the shared-device checks.
1. OwnTracks on both phones, each set up with the QR code from `uv run huvu tracker add <username>`, for the location checks.
1. To have something that needs an approval before any driver exists, put `"huvu.set_mode" = "ask"` under `[policy]` in `backend/huvu.toml`, and restart the backend. Then "huvu, set the house mode to away" needs a yes.

## Logging in

1. Open the app on a phone for the first time. You should see the login screen.
1. Type a wrong password. You should see a message that says so, and you stay on the login screen.
1. Type the right password and tap "come in". You should see the greeting, for example "Ready?".
1. Close the app fully and open it again. You should not have to log in again.
1. Do the same on the laptop. The laptop should get its own place in the device list (see "The menu").
1. On the laptop, type a wrong password ten times. The next try should say there were too many wrong passwords, even with the right password. The phone on mobile data should still log in. The laptop should log in again after 15 minutes.
1. Turn off the phone's Wi-Fi and open https://huvu.hrishik.me in its browser. You should see the same login screen, with a padlock in the address bar. Log in and ask something. huvu should answer.
1. Open the menu, tap log out, and open the app again. You should see the login screen.

## Talking to huvu in writing

1. Write a question and send it. On the phone, send with the send button; Enter makes a new line. On the laptop, Enter sends. Your words should fly up to the top next to "YOU", in plain letters. huvu's answer should write itself in, in upright serif letters.
1. Ask huvu to remind you of something every day at a time, such as "remind me to water the plants every day at 9". Within a few seconds it should say it's on it, and within about ten it should say it's done. The reminder should show in the menu under your triggers.
1. Ask something that needs thought, such as a small puzzle. While huvu works, a faint line of its thinking should show under the answer and change as it thinks. It should fade away once the answer is done. (Only with a model that sends its thinking, such as gpt-6-sol.)
1. Say "turn on the night sky". The night sky should come on within a few seconds, with no faint thinking line under the answer.
1. Ask something, then say "turn off the night sky", then ask "what did I just turn off?". huvu should know, even though a quicker model handled that command.
1. Say "turn it off" right after a device command. huvu should know what "it" is, with the faint thinking line: an ask that points back goes to the model that has the conversation.
1. Ask something quick that isn't about the house, such as "what's the temperature today?". It should answer in a few seconds, with no faint thinking line.
1. Turn the home theatre's AV unit on and wait a minute. Tell huvu you're in the home theatre and about to watch something. Then say only "open YouTube". YouTube should open on the projector within a few seconds, and huvu should not ask which room or which device. Then say "play Interstellar on Netflix". It should start on the screen. huvu can't see the screen, but it hears what Alexa said back, so within about ten seconds it should tell you what Alexa answered, such as "Here's Interstellar".
1. With the AV unit on, ask huvu for something on the TV that Alexa can't do, such as "open the app called Zorblat on the TV". huvu should tell you what Alexa said, such as that it couldn't find it. It should not say it's done.
1. With the home theatre off, tell huvu "turn on the home theatre". The strip lights and the focus lights should come on within a few seconds. The AC's switch should come on, and the AC should start a few seconds after it connects to its Wi-Fi, which can take up to two minutes. The AV unit should come on too: the projector lights up and the amp's volume climbs, about half a minute later. Once the Fire TV has started and shows its home screen, huvu should open on it by itself and stay in front. huvu should not ask what you meant.
1. With the home theatre on, tell huvu "turn off the home theatre". The strip lights, the focus lights and the night sky should go off within a few seconds, the AC should stop, its switch should go off about five seconds later, and the projector should go dark. The amp should go off too. The AV unit's power should stay on for about two minutes while the projector cools, and only then go off.
1. With the AV unit on, note the volume on the amp's display, then tell huvu "turn the volume up by 10 points". The number on the amp should climb by 10 within about ten seconds, and huvu should answer in one go, without saying it worked in batches. Then say "turn it down by 2.5". It should drop by 2.5.
1. Tell huvu "turn off the home theatre", and within two minutes, before the AV unit's power goes off, tell it "turn on the home theatre". The projector should go dark first. The Fire TV loses power when the two minutes end and starts again. Once its home screen shows, huvu should open on it by itself, within about five minutes of the second ask.
1. With the AC's switch on, tell huvu "turn off the AC switch". The switch should go off within a few seconds, and huvu should not say it can't find it. Tell huvu "turn the AC switch on" and it should come back on.
1. With the home theatre's AC off, tell huvu "set the home theatre AC to 24". The AC should beep and start within a few seconds, and huvu should say it's on at 24. Ask "how warm is it in the home theatre?" and huvu should give the room's temperature. Then turn the AC off with its own remote and ask huvu whether it's on. It should say it's off. Ask for 23.5 and huvu should say the AC takes whole degrees only.
1. With the AV unit off, ask huvu to open YouTube. huvu should say the AV unit is off. Nothing should turn on by itself.
1. Hint at what you want without naming a device, such as "it's stuffy in here". huvu should still do it, a little slower.
1. Ask something that makes huvu search the web more than once, such as "what did OpenAI announce yesterday, and what does it mean?". Each "searched the web" line should show where huvu searched: after the words it said before searching, and before the words it wrote after. The lines should not all gather at the top of the answer.
1. While huvu answers, look at the garden. A little more should grow as the words come in, slowly enough to watch each petal paint in. On a phone, typing while it grows should feel as quick as typing with nothing growing.
1. While huvu answers, look at the closed flower by the text field. It should turn slowly until the answer is done, then settle with its petals where they started.
1. Ask for something long, such as a recipe, and scroll down through the answer, then back up to the top. The answer should stay on screen. It should not jump to the history while you read.
1. Read a few paragraphs of that long answer. The letters should be smaller than in a short answer and the lines well spaced, easy to read for a minute or two without strain.
1. At the top of the answer, scroll up once more. The screen should fold into a list of all earlier questions, one per line. Tap one. It should open large again. Scroll again, and it folds back.
1. Ask something, then tap "stop" while huvu answers. The button should say "stopping", and the answer should end a moment later. huvu may finish its sentence first. That is expected.
1. Tap "edit" next to one of your earlier questions, change it, and send. That question and everything after it should go away, and huvu should answer the new question.
1. Ask something that makes huvu offer choices, such as "help me pick a movie, give me three options". Tap one choice. It should be sent as your next question.
1. Ask huvu to open a web page and show you a screenshot of it, such as "open wikipedia.org and show me what it looks like". The screenshot should show under the words huvu wrote before it, whole and not cut off. You should see no "MEDIA:" text and no file path. In history, the turn should show an "1 image" chip.
1. Ask huvu to show you a picture from the web by its address, such as "show me this image: https://upload.wikimedia.org/wikipedia/commons/a/a7/Camponotus_flavomarginatus_ant.jpg". The picture should show in the answer.
1. Ask huvu to show you a file that isn't a picture, such as "show me /opt/data/huvu/huvu.toml as an image". No picture should show. huvu should say it couldn't, or the path should stay as plain text.
1. In a conversation from a day or more ago, ask about something you said early on, such as "what was that recipe we talked about yesterday?". huvu should find it, even if a lot was said since.
1. Turn the backend off and ask something. The answer should fail and show "try again". At the top, between "huvu" and the live feeds, "can't reach huvu" should show in red. Turn the backend on and tap "try again". huvu should answer, and the red line should fade away.
1. With the backend off, close the app and open it again. It should open as usual, with "can't reach huvu" at the top. It should not freeze or close by itself. Turn the backend on. Within half a minute the red line should go, without you doing anything.
1. Open https://huvu.hrishik.me in a phone's browser (Chrome on Android, Safari on an iPhone). The text field should say "write, or tap the flower", not "hold space to speak". The keyboard should not pop up by itself. There should be a send button, and the keyboard's Enter should make a new line.
1. Open it on a laptop. The text field should say "write, or hold space to speak", and typing should go straight into it without a click.

## Coming back to the app

1. Ask something long, and close the app while huvu still answers. Open it again. The answer should still be coming in, or be complete.
1. Close the app with everything finished, and open it again. You should see a greeting, not the old answer. Scroll up, and the earlier questions should be in the history.
1. Restart the backend while the app is open. Within half a minute, the app should be connected again without you doing anything. Ask something to check.
1. On the phone, after the host was rebuilt with a change you can see: open the app, switch to another app, then come back. Do it a second time. The change should now show, without installing anything.

## Talking to huvu by voice (laptop, in Chrome)

1. Click the flower. It should move to the middle of the screen, and the text field should go away.
1. In the browser's site settings, block the mic for huvu, then click the flower. Under it, it should say to allow the mic, and it should not keep trying. Allow the mic again and click the flower. It should listen.
1. Stay quiet and watch the flower listen for about ten seconds. Each ring it sends out should fade in as it leaves the flower and fade out as it grows, with no ring popping in at full strength.
1. Say a question with your name and "huvu" in it. The flower should grow while you talk, and no words should appear while you speak. About three seconds after you stop, when the mic stops, your words should appear at once, with your name and "huvu" spelled right, fly up, and huvu should answer. On a phone too, there should be no wait between the mic stopping and your words.
1. Listen for huvu saying its own name. It should say "hoo voo".
1. Listen to huvu's answer. It should start speaking within a second or two of its first words on screen, and keep speaking without gaps between sentences, while the rest of the answer is still being written.
1. Ask something by voice that might tempt an emoji, such as "say something cheerful about my morning". The spoken answer, and its words on screen, should have no emojis, no bullet points and no bold text. Switch back to typing and ask the same: emojis and formatting are allowed again.
1. Click the flower and ask something with a two-second pause in the middle, as if you stopped to think. It should keep listening through the pause and send the whole question.
1. Hold the space bar, talk, and let go. Your words should go up when you let go, even if you paused while holding, and huvu should answer.
1. After huvu answers a question asked with space, stay quiet. It should not start listening by itself.
1. Ask with space, and talk while huvu speaks. huvu should keep speaking. Hold space while huvu speaks. huvu should stop at once and listen to you.
1. Write a question and send it, and while huvu's answer still writes itself in, tap space once with the text field empty. The answer should keep coming. Then hold space and talk. The answer should stop, and huvu should listen to you.
1. Press Tab until the flower has a ring around it, then hold space, talk, and let go. It should be the same as holding space anywhere else, not the same as clicking the flower.
1. Ask by voice something that makes huvu search the web, and stay quiet while it searches. huvu should say the line before its search, go quiet while it searches, and then say the rest of the answer.
1. Click the flower and ask something that needs a tool, such as the weather. Once you stop talking, the flower should half close, turn slowly, and glow red behind. It should keep doing that through every quiet moment while huvu works, also after huvu says it's on it. When huvu's voice plays, the flower should open, sway and glow gold. Once huvu has finished and listens again, it should send out rings and never turn.
1. While huvu speaks, look at the garden. It should grow about as slowly as huvu talks, not race ahead of it.
1. Click the flower, ask something, and talk while huvu speaks. huvu should stop speaking and listen to you again by itself.
1. Scroll the answer. The flower should shrink and dock at the bottom. Tap it, and it should come back to the middle.
1. Tap "write". The text field should come back, and the conversation should be the same as before.
1. On the phone, tap the flower and ask something that makes huvu work for ten seconds or more, such as the weather. Listen to the start of huvu's answer. Its first words should be heard whole, with nothing cut off.
1. On the phone, turn the backend off and tap the flower. Under it, it should say "huvu isn't answering", and it should stop listening. No question should appear on the stage. Turn the backend on and tap the flower again. It should listen.
1. Turn the backend off while huvu thinks about a spoken question. The answer should fail and show "try again", and the flower should stop listening. It should not ask again by itself.

## Notifications

1. With the app open, ask huvu: "send me a notification that says hello". The notice should appear on the stage marked "from huvu", in huvu's slanted letters.
1. Ask huvu something long, and while it answers, have it send a notification some other way (for example from a trigger). The answer should keep the screen. The notice should wait in the history under it.
1. Close the app, or switch to another app, and get huvu to send you a notification. Your phone should show it like any app notification.
1. Tap that notification. huvu should open and show the notice.
1. Open huvu yourself, without tapping. The notification should leave the phone's notification list, since huvu shows it on its own screen.
1. Check the other person's phone. A notification meant for you should not appear there. A notification for the whole household should appear on both.
1. Ask huvu: "tell mom I'll be late" (use whatever you call the other person). Their phone should get "I'll be late" or similar, marked as from you, by the name they call you. With the app closed, their phone's notification should have that name as its title. Your phone should get a short note from huvu that it was sent.
1. Tell huvu: "I call mom Amma" (use a new name for the other person). Then ask it to "tell Amma I'm home". It should reach the same person. When they message you, your phone should show it from "Amma".
1. Ask huvu: "tell everyone dinner is ready". Everyone else in the household should get it. Your own phone should get only huvu's note that it went.
1. Ask huvu to send you a notification with a question, such as "ask me tomorrow morning what I want for lunch". When it arrives, answer in the app with only the answer ("dosa"). huvu should know what you are answering.

## iPhone: huvu on the home screen

There is no iPhone app. On an iPhone, huvu is the web page added to the home screen, and it gets notifications from there (iOS 16.4 or later).

1. On the iPhone, open https://huvu.hrishik.me in Safari. Tap the share button, then "Add to Home Screen". The icon should be huvu's, with the flowers, named "huvu".
1. Open huvu from that icon. It should open full screen, with no Safari address bar, and ask you to log in, even if you logged in on Safari before. Log in.
1. Open the menu. Under "you", it should say "on iPhone" and "notifications off", with "turn on" next to it.
1. Tap "turn on". iOS should ask whether huvu may send notifications. Allow it. The menu should say "notifications on".
1. Close huvu, and get huvu to send this person a notification (from another phone: "tell mom I'm home"). The iPhone should show it on the lock screen, titled with the sender's name.
1. Tap it. huvu should open and show the notice.
1. With huvu open on the iPhone, send another. It should show on huvu's screen. A banner from iOS may flash for a moment, and should not stay in the notification list.
1. From the TV's "who's here", pick this person. The iPhone should get "Is it you?" with huvu closed. Tap it, and the card should be waiting. Tap yes, and the TV should say this person is there.
1. With huvu closed on the iPhone, ask for something that needs this person's yes. The iPhone should show "huvu needs a yes".
1. In a Safari tab (not the home screen icon), log in. The menu should show no notifications row, and say "on Safari on iPhone".

## Approvals (phones only)

1. Ask huvu: "remind me every day at 9pm to drink water, and use the advanced model for it". A card should appear above the text field. It should say "approve?", what the action does, who asked, and a countdown.
1. Tap "approve". The card should go away, and the reminder should show in the menu under your triggers as watching.
1. Do it again, and tap "deny". The card should go away, and the reminder should not start. Delete the reminders from the menu afterwards.
1. Ask from the laptop instead. The card should appear on your phone, not on the laptop.
1. With the app closed, ask from the laptop. Your phone should show a notification that huvu needs a yes. Tap it. The card should be waiting in the app as it opens, within a second. Do the same with the app in the background instead of closed.
1. Let one card run out without answering. It should go away when the countdown reaches zero.

## Triggers

1. Ask huvu: "remind me in two minutes to check the oven". After two minutes, you should get the reminder.
1. Ask huvu to set up something that repeats, such as "every day at 9pm, tell me to take my medicine".
1. Open the menu. Under triggers, both should be listed, each with what it does and how often: the oven reminder "once", the medicine "every time".
1. Tap "pause" on one. It should say paused by you. Tap "resume". It should be live again.
1. Ask huvu: "what triggers do I have?". It should name the same ones.
1. Log in as the other person. Your triggers should not appear in their menu.
1. Ask huvu to set up something that needs its strongest thinking, and say it should use the advanced model. On your phone, a card should ask you to allow it. In the menu it should say it waits for your yes. Tap "allow" (or yes on the phone). It should say watching, with "advanced" after how often.
1. Ask for another advanced trigger, and tap no on the phone. It should leave the menu.
1. In the menu, your dream should be listed: "Your dream...", every time, on pro. The next morning, a notification from huvu should wait for you: a few short lines of notes about the night, not paragraphs. If huvu set up a trigger on its own that night, the notes should name it and say why, and it should be in your menu.
1. Ask huvu to pause your dream, then to remove it. Each time it should say only you can, from the app, and the dream should stay in the menu. Pause it from the menu: that should work. Resume it.

## Wakes: huvu at work in the background (phone and laptop)

1. Ask huvu to set up a trigger that, in two minutes, looks something up and asks you a question about it before it goes on. When it fires, a notification should come. Tap it. The app should open on that wake: a line at the top says what it waits for, with "back" and "end", and the turn under it is what huvu did.
1. Type your answer there. huvu should carry on in the same wake, and not in your main conversation. There should be no "edit" on what you typed.
1. Open the menu. Under "at work", the wake should be listed while it runs or waits for you. Once it is done, it should leave the list.
1. Tap "back". Your main conversation should come back as you left it.
1. Open a waiting wake and tap "end". It should say ended, the text field should go, and it should leave the menu.
1. Open a wake that finished on its own. The top should say "done, reply to carry on", and the text field should stay. Type something. huvu should carry on in that wake. A day later, the text field should be gone.
1. Leave a wake waiting for over an hour, then answer it. huvu should still know what it was doing.
1. Leave a wake waiting for a day. It should leave the menu by itself.
1. Send mom a message from a wake. Her notification should open nothing special.
1. On the TV, there should be no wakes.
1. In the chat, ask huvu to look something up in its browser. It should open the browser in the same turn, with no trigger and no card on your phone. The turn should show one "used the browser" note with "watch", however many steps huvu takes.
1. Tap "watch" while it works. huvu's browser should show, live, as huvu clicks through. Tapping or typing on it should do nothing. Tap "close".
1. Watch again and tap "take over". A red edge should show around the screen, and the top should say you're driving. Click and type in the browser. It should respond to you. On the phone, tap "keyboard" to type.
1. While you drive, open a page that plays sound. You should hear it, a second or two behind the picture.
1. Tap "hand back". The red edge and the sound should go, and huvu should carry on in the browser when you ask it to.
1. Take over, then tap "close" without handing back. Ask huvu to use the browser again. It should work, since closing hands it back.
1. Ask huvu to log in somewhere that asks for a captcha or a code. It should stop at that step and ask you to take over. In a wake, a notification should come.
1. If huvu says it handed a browser task to the background, a wake should show under "at work" at once, with no card on your phone. Open it and tap "watch". The browser should show the same way.
1. Log in to a site in that browser through huvu once (it asks you for the OTP). Ask for a second task on the same site a day later. It should still be logged in.
1. On the TV, a turn where huvu used the browser should show "used the browser" with no "watch".
1. Tap "watch" on a wake that isn't using the browser. It should say the browser isn't open right now.

## The wiki (phone and laptop)

huvu keeps what it learns about you in a wiki of its own. It writes most of it at night.

1. Open the menu. Under "wiki", tap "open". The wiki should cover the whole screen, with an index (huvu's list of its pages, each with what it holds) and every page, newest first.
1. Tap an index line or a page. The page should open as huvu's writing, with its headings, lists and tables. A link to another page should open that page in place. A link to a page that doesn't exist yet should look faded and do nothing.
1. Press back (the back button on the phone, Escape on the laptop). It should go back one step: from a page to the list, then out of the wiki.
1. On a page, tap "edit". The page's text should appear for editing. Change a word and tap "save". The page should show your change.
1. Edit a page, and before saving, ask huvu in another tab to change the same page. Then save. It should say huvu changed the page while you edited, and let you keep yours or take huvu's.
1. Tap "new page", type a name, write something and save. The page should appear in the list.
1. On the TV, there should be no wiki.

## The menu (phone and laptop)

1. Tap "huvu" at the top left. The menu should open with your name, this device's name, your triggers and your devices.
1. Tap anywhere outside the menu. It should close. On the phone, the back button should also close it.
1. Under devices, log out the laptop from the phone. On the laptop, the next thing you do should send it back to the login screen.
1. Turn the backend off, close the app and open it again, then open the menu. Where your name, triggers and devices go, it should say they are unknown until huvu answers. It should never say "None yet". Turn the backend on and open the menu again. Your name and lists should be there.

## The TV: pairing

1. Install huvu on the TV (see `hrishik.md`). It should show in the TV's apps with its own tile, and open with the remote's select button.
1. Open huvu on the TV for the first time. It should say "Pair this screen." and show a code such as ABCD-EF23.
1. On your phone, open the menu, pick "pair a device", and type the code. Within a few seconds, the TV should show the greeting with an Alexa example under it.
1. Leave a code on the TV for more than 10 minutes without typing it. The TV should change to a new code by itself.
1. Type a wrong code on the phone. The phone should say no device shows that code.
1. Log in as a guest, if you have one. "pair a device" should not be in their menu.

## The TV: asking through Alexa

1. With huvu open on the TV, hold the remote's mic button and say "Alexa, ask my huvu what time is it". Alexa should say nothing, and huvu should show your ask on the TV and answer it out loud.
1. While huvu is still talking, press back. It should go quiet.
1. Open YouTube on the TV, then tell huvu "turn on the home theatre" from your phone. Within a few seconds, huvu should come to the front on the TV over YouTube.
1. Close huvu on the TV and ask again. Alexa should say huvu isn't open on the TV.

## The TV: who's here

1. Press the Menu button on the remote. A list should open with "who's here" and the family's names.
1. Pick your name. The TV should say "<your name>, check your phone". Your phone should show "is it you?" with "Is it you at the living room?".
1. Tap "yes, it's me". The TV should say "with <your name>".
1. With the app closed, do it again. Your phone should show a notification that asks if it's you.
1. Pick the other person's name and have them tap "yes, it's me". The TV should now be with them, not you.
1. On your phone, open the menu. Where you're recognized should show the TV. Tap "not there". The TV should stop saying your name.
1. Leave the TV alone for 10 minutes after someone is recognized. It should stop saying their name.
1. On the TV, use the D-pad to move through the list, and the back button to close it.

## Location (OwnTracks)

1. Scan the QR code with the phone's camera and open the link. OwnTracks should open with its settings filled in, in HTTP mode.
1. In OwnTracks, tap the button that sends your location now. A few seconds later, open its list of regions. You should see `home` and `near_home`.
1. Walk or drive out of home. Ask huvu "where am I?". It should say you're outside home, or in near_home.
1. Come back home. Within a minute of arriving, ask huvu where you are. It should say home.
1. From the other phone, ask huvu where you are. It should say it can't tell, since your location is yours alone.
1. Ask huvu "remember this place as the corner shop" while you stand somewhere new. Within about 15 minutes, `corner shop` should appear in OwnTracks's regions on your phone, and not on the other phone.
1. Ask huvu to share the corner shop with the household. Within about 15 minutes it should appear on the other phone too.
1. Ask huvu to forget the corner shop. Within about 15 minutes it should go from OwnTracks's regions, on Android and on iPhone.
1. Force stop OwnTracks from Android's app settings, and open it again. If its regions are gone, tap the button that sends your location now about every 15 minutes. Within about 45 minutes `home` and `near_home` should be back.
1. Turn on flight mode, walk out of home, walk back in, and turn flight mode off. huvu should still learn that you left and came back, a little late.
1. Run `uv run huvu device list` on the backend. Each tracker should be listed with the kind `tracker`.

## Browsing (Chrome extension, laptop)

1. On the backend, run `uv run huvu tracker add <you> --browser`. It should print an address and a token.
1. In Chrome, open `chrome://extensions`, turn on Developer mode, choose "Load unpacked" and pick the `browser-extension` folder. huvu's icon should appear in the toolbar.
1. Click the icon. A small window should open, saying it sends the titles of the pages you open. Click Settings in it. The options page should open. Paste the token and press Save. It should send nothing from before this moment. Open a page, wait a minute and press Send now: it should say it sent a visit.
1. Click the icon and choose "Pause 30 min". The icon should show two bars, and hovering it should say until when. Open a page, wait two minutes and press Send now on the options page: no visit should be sent, and the options page should say it is paused. Click the icon and press Resume. The bars should go, and a page opened after that should be sent within a minute or two. The page opened during the pause should never be sent.
1. Click the icon and choose "Pause", with no timer. Close Chrome and open it again. The icon should still show the bars, and nothing should be sent until you press Resume.
1. After a code change in the extension, press its reload button in `chrome://extensions`. Chrome doesn't reload an unpacked extension by itself.
1. Open a YouTube video. About a minute later, ask huvu "what did I just watch on YouTube?". It should name the video.
1. Search Google for something unusual. A minute later, ask huvu what you searched for. It should say the words.
1. Ask huvu for the address of the last page you opened. It should say it only knows the page's title, never its address.
1. Open Gmail. Ask huvu whether you opened your mail in the last few minutes. It should say it can't see that.
1. Open a page in an incognito window. huvu should never learn of it.
1. From another family member's phone, ask huvu what you watched. It should say it can't tell, since your browsing is yours alone.
1. Stop the backend for a few minutes, browse, and start it again. Within a minute of the start, huvu should know the pages from while it was down.

## Mail (bills you forward)

1. Swiggy's account email is `bills@mail.hrishik.me`. Order something on Swiggy. The bill should reach your Gmail as before, and within a minute huvu should have it too: ask huvu "what was my last bill for?". It should say the shop, the items and the total, read from the PDF.
1. Ask huvu where the PDF is. It should give a path on the host. Ask it to upload the bill somewhere in its browser. It should be able to.
1. From another email account, send a mail to `bills@mail.hrishik.me` that claims to be from the shop. Ask huvu about it. It should say it can't trust that the shop sent it.
1. From another family member's phone, ask huvu about your bills. It should say it can't see them.
1. Stop the backend and forward a bill from Gmail to `bills@mail.hrishik.me`. No bounce should come back. Start the backend and forward it again. huvu should have it once.

## Swiggy (food and Instamart)

1. Ask huvu to find you something for lunch on Swiggy and add it to your cart. Open the Swiggy app. The cart should hold what huvu picked.
1. Ask huvu to place the order, then to pay for it, and then to order cash on delivery. Each time it should say only you can place an order, and the Swiggy app should show no new order.
1. Ask huvu what your last Swiggy order was. It should name it, with the total.
1. Ask huvu, in a chat, to order food on swiggy.com in its browser. It should stop before placing the order and hand the screen to you.

## What doesn't work yet (don't test)

1. Voice on the phone: it works only while Groq answers. When Groq's rate limit hits, an ask on the phone is lost, and the flower says huvu couldn't hear it.
1. Bringing huvu to the front when Alexa is asked while another app is open. The launch over ADB isn't built yet.
1. Live camera feeds. The list may appear, but no feed can play yet.
1. The doorbell. It waits for the Tuya driver.
1. The house's devices, such as "turn on the bedroom light when I walk in". They wait for the Home Assistant setup (`home-assistant.md`). Once it is done: walk into the room, and the light should come on within a second or two.
1. An iPhone app. iPhones use huvu from the home screen (see above).
1. Phone notifications, until the Firebase project is set up (see `v0.md`). Before that, notices, approvals and questions show only while the app is open.
