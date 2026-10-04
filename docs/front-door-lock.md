# Front door lock

Findings from testing the front door lock against Tuya's cloud API on 2026-10-02. The lock is the only doorbell huvu has, so this decides how the doorbell reaches the app.

## Outcome

The doorbell gives huvu a snapshot per press. It can't give a live feed.

What works, all on Tuya's free trial tier:

1. Doorbell presses, unlocks (with method and enrolled slot), battery level and alarms reach the cloud about a second after they happen.
1. Each doorbell press uploads an encrypted JPEG. The gateway can fetch it and decrypt it. This was tested end to end.
1. Remote unlock exists, but only as an answer to a request that a visitor starts on the lock. It was not tested.

What doesn't work, and why, is in [Ruled out](#ruled-out).

## The device

| Field | Value |
|---|---|
| Name in Smart Life | Front Door Lock |
| Product | TX7916, product ID `ymbdzahss78wht6n` |
| Tuya category | `jtmspro` (Wi-Fi lock with video) |
| Device ID | `d7f016249308a67f98dhbc` |
| Account UID | `in1781196159380T8oWu` |
| Power | Battery |

The lock keeps Wi-Fi off while it sleeps. It comes online for an event and goes offline after. In the test press, the online event came at 19:55:42 and the doorbell event at 19:55:43. Anything that needs the lock to answer (a stream, a command) fails while it sleeps. Events and media still arrive, because the lock pushes them when it wakes.

The camera has no device ID of its own. Every call uses the lock's ID.

## Tuya cloud project

1. Created on platform.tuya.com, data center India. API base is `https://openapi.tuyain.com`.
1. Smart Life is linked to the project by QR code, so the lock stays paired to the Smart Life account. Re-pairing would change nothing here, but it would break any local key saved before it.
1. Services subscribed, all free trials: IoT Core, Authorization Token Management, Smart Lock Open Service, IoT Video Live Stream. Live Stream is not needed now, since the lock rejects every stream call.
1. Credentials live in `~/.tuya.env` on the dev laptop (`TUYA_ACCESS_ID`, `TUYA_ACCESS_SECRET`, `TUYA_DEVICE_ID`, `TUYA_ENDPOINT`), mode 600. They are never committed. The Access Secret controls the front door, so treat it like a house key.
1. No credit card was asked for. Trials expire and are extended by hand in the console, so check each service's expiry date under Cloud > Cloud Services.
1. IoT Core trial limits: about 26,000 API calls and 68,000 messages a month. React to pushed events and don't poll.

### Request signing

Every call carries the headers `client_id`, `t` (ms timestamp), `nonce`, `sign_method: HMAC-SHA256`, `sign`, and (after the token call) `access_token`.

```
stringToSign = METHOD + "\n" + sha256hex(body) + "\n" + "" + "\n" + pathWithQuery
sign         = upper(hex(HMAC-SHA256(accessSecret, accessId + accessToken + t + nonce + stringToSign)))
```

`pathWithQuery` needs its query keys in alphabetical order. The token comes from `GET /v1.0/token?grant_type=1`, signed with an empty `accessToken`.

## What the lock reports

From `GET /v2.0/cloud/thing/{device_id}/model` (the `model` field is a JSON string inside the JSON, so parse it twice) and a week of logs from `GET /v1.0/devices/{device_id}/logs`. The model is Tuya's template for the category, so some codes never fire on this hardware.

| Code | Type | Meaning | Observed |
|---|---|---|---|
| `doorbell` | bool | Doorbell pressed | Fires on each press |
| `unlock_fingerprint`, `unlock_face`, `unlock_password`, `unlock_card`, `unlock_temporary`, `unlock_app` | number | Unlocked with this method. The value is the slot the lock assigned at enrolment | Fingerprint and face seen |
| `update_all_finger`, `update_all_face` | list | Every enrolled slot | Fingerprints 16, 17, 11, 12, 14, 15. Faces 18, 22, 10 |
| `open_inside` | bool | Opened from the inside handle | None in a week, unclear if it fires |
| `unlock_request` | number | A visitor asked for remote unlock. The value is a countdown, at most 90 s | Not tested |
| `alarm_lock` | enum | `low_battery`, `alarm` (tamper), `auth_error`, `strager_wonder` (lingering; Tuya's spelling) | Stored value is a stale `low_battery`. React to new events only |
| `residual_electricity` | number | Battery percent | Always 70 for a week, so it reports in coarse steps |
| `lock_motor_state` | bool | Bolt state | Never reported. huvu can't know if the door is locked |
| `video_request_realtime` | raw | Lock wants a live view (fires with the doorbell) | Fires on each press |
| `initiative_message` | raw | Base64 JSON pointing at the media the lock uploaded | Fires on each press |
| `photo_again` | bool, writable | Take another photo | Ignored while asleep, see below |

Slot numbers map to people by hand once, in the gateway's config. The unlock history API (`GET /v1.1/devices/{device_id}/door-lock/open-logs`) carries the names set in Smart Life, if any.

## Doorbell press, step by step

1. The lock wakes and reports online.
1. It reports `doorbell: true` and `video_request_realtime`.
1. About a second later it reports `initiative_message`. Decoded, that is `{"cmd": "door_lock_video", "files": [[bucket, path, key, expiry], ...]}` with one `.jpg` and one `.mjpeg`. The bucket is `ind-tuya-bizlock` on S3 `ap-south-1`.
1. The JPEG shows up as the newest entry in the lock's album, with `event_type: 18` (`LOCK_DOOR_BELL`).

## Getting the snapshot

1. `GET /v1.0/smart-lock/devices/{device_id}/albums-media`. Each entry in `result.album_list` has `file_url` (a presigned S3 URL, valid for an hour), `file_key` (16 characters), `event_type` and `upload_time`. The newest entry comes first. Free storage keeps 3 days (`order_code: cloud_free_storage_3day`).
1. Download `file_url`. The body is encrypted.
1. Decrypt with AES-128-CBC and PKCS7 padding. The key is `file_key` as UTF-8. The file starts with a 64-byte header: bytes 0 to 3 are unused, bytes 4 to 19 are the IV, and bytes 20 to 63 are unused. The ciphertext starts at byte 64.

The result is a 480x320 JPEG. The test press gave a 9,920-byte download and a 9,843-byte image.

`GET /v1.0/devices/{device_id}/door-lock/latest/media/url` returns empty fields for doorbell presses. It only covers remote unlock (`file_type=1`) and alerts (`file_type=2`).

Tamper and lingering alerts upload a photo to the same album, so the same path serves them.

## Real-time events

Not built or tested yet. Tuya pushes device events through its Message Service, which runs on Apache Pulsar.

1. Enable Message Service in the cloud project and select `dp_report`. Online and offline messages are optional and only show the lock waking.
1. Connect to `pulsar+ssl://mqe.tuyain.com:7285/`, user is the Access ID, password is the Access Secret.
1. Messages are Base64, AES-128-ECB, with the middle 16 characters of the Access Secret as key (per Tuya's docs). Newer projects may send AES-GCM; `lib/tuya_iot/openmq.py` in xtend_tuya handles both and is a good reference.
1. Pulsar holds messages until they are acknowledged, so a gateway restart loses nothing.
1. Only one consumer reads a project's Pulsar queue, so a second reader of Pulsar needs its own cloud project. Home Assistant's `xtend_tuya` does not use Pulsar: it reads events over Tuya's MQTT channel (`/v1.0/iot-03/open-hub/access-config`), so it can share this project with the gateway. Both draw on the same monthly quota.

On `doorbell`, wait for the album to show an entry newer than the press (it took about a second), then fetch and decrypt it.

## Remote unlock

`GET /v1.0/devices/{device_id}/door-lock/remote-unlocks` returns `remoteUnlockWithoutPwd`, turned on. The flow is `POST /v1.0/smart-lock/devices/{device_id}/password-ticket`, then `POST /v1.0/devices/{device_id}/door-lock/password-free/open-door` with the `ticket_id`. If that fails, try `POST /v1.0/smart-lock/devices/{device_id}/password-free/door-operate`.

The lock only takes this while it is awake, in practice inside the `unlock_request` countdown a visitor starts on the lock. huvu can't unlock the door at any other time. None of this was tested. Any unlock from huvu must need a human tap; a model or a face match alone must never open the door.

## Ruled out

1. **Live video through the cloud API.** RTSP and HLS (`POST /v1.0/users/{uid}/devices/{device_id}/stream/actions/allocate`) failed with `100003` (external service error) before and during a press. WebRTC (`GET /v1.0/users/{uid}/devices/{device_id}/webrtc-configs`) fails with `115029` (device not supports). go2rtc's Tuya source uses that WebRTC path, so it fails too.
1. **Live video the way Smart Life does it.** Smart Life opens a P2P connection to the lock through Tuya's mobile App SDK (`ThingSmartWiFiLockDevice`, `p2pConnect()` then `startPreviewWithDefinition()`), and only while the lock is awake after an event. huvu could use the same SDK, but the lock would have to leave Smart Life and pair under huvu's own accounts, and it's native code on each platform. Capturing Smart Life's screen on a spare Android phone was also considered. Both were judged not worth the effort.
1. **The MJPEG clip.** The lock uploads a short clip with each press, and `initiative_message` gives its path and key. S3 URLs are signed per file, the album returns only the JPEG for doorbell events, and no cloud API was found that signs the clip's path.
1. **Snapshot on demand.** `photo_again` sent while the lock slept was accepted by the cloud (`result: true`) and did nothing in the next 75 seconds. It may work inside the wake window after a press, which was not tested. The cloud may also hold the command and run it at the next wake, so one stray photo after the test is expected.
1. **tuya-local or LocalTuya.** They need the lock reachable on the LAN, and it sleeps. They can't fetch the album or run the unlock ticket flow either. They suit the always-on Tuya switches in the house, not this lock.

## Home Assistant, for reference

The `xtend_tuya` integration (HACS) supports `jtmspro` through the cloud: sensors for every code above, and remote unlock through the same ticket flow. It has no album or media decryption code. Its camera entity for the lock uses WebRTC, so it would show no picture. If HA is ever used, the gateway still fetches snapshots itself.

## What this means for huvu

The doorbell can't be a `video_feed` part, and it shouldn't be in the gateway's live feed list. A doorbell press should reach huvu as an event with an image part (the decrypted snapshot) and the time, plus the person's name when the event is an unlock with a known slot. The gateway owns the Tuya client: signing, the Pulsar consumer, the album fetch and the decryption.
