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
1. In a new conversation (after five minutes of quiet), tell huvu to work a device by its name, such as "turn on the night sky". The answer should start within about two seconds, with no faint thinking line under it.
1. Right after that, ask something that needs thought, such as "plan a movie night for saturday". The faint thinking line should show. Then tell it to work a device again, such as "turn off the night sky": it should be quick again, with no thinking line. Then ask "what did I just turn off?". huvu should know, even though a quicker model handled that command.
1. Ask something quick that isn't about the house, such as "what's the temperature today?". It should answer in a few seconds, with no faint thinking line.
1. Hint at what you want without naming a device, such as "it's stuffy in here", or misspell one, such as "projectr on". huvu should still do it, a little slower, with the faint thinking line: anything that isn't a plain command goes to the model that thinks.
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
1. In the menu, your dream should be listed: "Your dream...", every time, on pro. The next morning, a notification from huvu should wait for you: a few short lines of notes about the night, not paragraphs.

## The menu (phone and laptop)

1. Tap "huvu" at the top left. The menu should open with your name, this device's name, your triggers and your devices.
1. Tap anywhere outside the menu. It should close. On the phone, the back button should also close it.
1. Under devices, log out the laptop from the phone. On the laptop, the next thing you do should send it back to the login screen.
1. Turn the backend off, close the app and open it again, then open the menu. Where your name, triggers and devices go, it should say they are unknown until huvu answers. It should never say "None yet". Turn the backend on and open the menu again. Your name and lists should be there.

## The TV: pairing

1. Open huvu on the TV for the first time. It should say "Pair this screen." and show a code such as ABCD-EF23.
1. On your phone, open the menu, pick "pair a device", and type the code. Within a few seconds, the TV should show the greeting with an Alexa example under it.
1. Leave a code on the TV for more than 10 minutes without typing it. The TV should change to a new code by itself.
1. Type a wrong code on the phone. The phone should say no device shows that code.
1. Log in as a guest, if you have one. "pair a device" should not be in their menu.

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
1. Ask huvu to forget the corner shop. On an iPhone it should go from OwnTracks's regions. On Android it stays in the list, and huvu ignores it.
1. Turn on flight mode, walk out of home, walk back in, and turn flight mode off. huvu should still learn that you left and came back, a little late.
1. Run `uv run huvu device list` on the backend. Each tracker should be listed with the kind `tracker`.

## What doesn't work yet (don't test)

1. Voice on the phone: it works only while Groq answers. When Groq's rate limit hits, an ask on the phone is lost, and the flower says huvu couldn't hear it.
1. Asking the TV through Alexa. The skill doesn't exist yet, so nothing appears on the TV unless a feed is picked. For the same reason, an approval for the whole household (asked at the TV with nobody recognized) can't be tested yet.
1. Live camera feeds. The list may appear, but no feed can play yet.
1. The doorbell. It waits for the Tuya driver.
1. The house's devices, such as "turn on the bedroom light when I walk in". They wait for the Home Assistant setup (`home-assistant.md`). Once it is done: walk into the room, and the light should come on within a second or two.
1. iPhones. No iOS build exists yet.
1. Phone notifications, until the Firebase project is set up (see `v0.md`). Before that, notices, approvals and questions show only while the app is open.
