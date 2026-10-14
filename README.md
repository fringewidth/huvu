# huvu

![huvu](banner.png)

huvu is a proactive agent harness that learns your behaviours and cares for you.

features:
- every action of your life is a data point stored in sqlite.
- exposed mcp to query this data.
- trigger engine that runs a script and/or invokes an agent when a certain condition on this data is met.
- nightly dreams that find patterns in this data and suggest triggers that will work for you.
- a wiki it maintains on your behaviours and preferences, that you can read and edit

huvu supports user groups, comes with a full fledged android app, and an iPhone home screen app that's equally capable. it runs hermes internally, so whatever works with hermes (e.g. plugins, MCPs), works with huvu.

huvu allows for pluggable "drivers", that act as sources of data about your life and behaviours to huvu. it learns from this data, and can act on changes here. when combined with hermes' ecosystem and memory, it acts as an extremely robust agent that doesn't need you to spoonfeed desiderata.

you only pay for hermes inference. everything else runs either locally or comfortably in the free tier of a host of providers.

## available drivers
- home assistant for home automation
- owntracks for private location tracking
- generic mail your agent can receive
- browser history

## features
- approval and transparency. every action exposed by the mcp is either silent, notify, or ask.
- hands free voice mode. currently runs on Whisper large-v3 served by Groq for STT and microsoft edge TTS. the entire setup is comfortably free.
- it's own linux machine with an xfce display and installed chromium that you can take over privately if needed
- really good UI. huvu paints a flower garland each session, which grows as it talks.
- dockerized. clone it. point claude/codex to it. you'll have it set up in five minutes tops.
- website, firetv app, android app and iphone (home screen) app.
- alexa skill receiver, so you can get alexa (from firetv for example) to ask things to huvu
- cache optimal model escalator
- give each family member their own agent. huvu supports user groups and has group scoped sessions for common devices (e.g., tv) with an option to escalate a session to your own user via phone approval.
- rich output support, including choices, image and video feeds

## upcoming
- immich driver
- widgets
- wake word support
- echo dot esque hardware
