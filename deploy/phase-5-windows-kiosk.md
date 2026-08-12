# Phase 5 — Windows kiosk hardening (MSI Cubi N100)

Goal: power on, and the wall is showing photos. No login prompt, no taskbar,
no screen blanking, no update reboot at 3am leaving a desktop on the wall.

Target URL: `http://192.168.1.218:3000` (immich-kiosk on the Synology).

**None of this has been tested** — it was written against Windows behaviour,
not against your Cubi. Work through it in order and expect one or two things
to need adjusting.

---

## 1. Check the URL works at all

Before automating anything, open Edge on the Cubi and go to
`http://192.168.1.218:3000`. You should get the slideshow.

If not, the problem is Phase 4, not this document — check the container is
running and that the Synology firewall allows 3000 on the LAN.

## 2. Auto-login

Without this the wall shows a login screen after every power cut.

```
netplwiz
```

Select your user, untick **Users must enter a user name and password to use
this computer**, Apply, and enter the password when prompted.

If that checkbox is missing (common on Windows 11), it is hidden until you
disable the passwordless-sign-in option:

```
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" /v DevicePasswordLessBuildVersion /t REG_DWORD /d 0 /f
```

Then re-run `netplwiz`.

> This trades physical security for unattended boot. Reasonable for a photo
> frame on your wall; do not do it on a laptop that leaves the house.

## 3. Stop the screen ever turning off

The single most common cause of "the wall is black".

```
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /hibernate off
```

Also disable the screensaver, which is separate from power settings:

```
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
```

Kiosk's own `KIOSK_DISABLE_SCREENSAVER` will **not** help here: the browser
wake-lock API requires a secure origin, and you are on plain HTTP over the
LAN. It fails silently. These OS settings are what actually keep the panel on.

## 4. Launch Edge in kiosk mode at logon

Use Task Scheduler rather than the Startup folder — it can retry, and it can
run whether or not the desktop has finished loading.

Create a task: **At log on**, action **Start a program**:

- Program: `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
- Arguments:

```
--kiosk http://192.168.1.218:3000 --edge-kiosk-type=fullscreen --no-first-run --disable-features=TranslateUI --disable-pinch --overscroll-history-navigation=0 --kiosk-idle-timeout-minutes=0
```

What each one is for:

| Flag | Why |
|---|---|
| `--kiosk` + `--edge-kiosk-type=fullscreen` | Full screen, no chrome, no address bar |
| `--no-first-run` | Suppresses the welcome/import wizard, which otherwise covers the wall on first boot |
| `--kiosk-idle-timeout-minutes=0` | **Important.** Edge kiosk resets the session after idle by default, which reloads and can flash the first-run UI |
| `--disable-pinch` | Stops a stray two-finger touch zooming the photo |
| `--overscroll-history-navigation=0` | Stops an edge-swipe navigating "back" to a blank page |

In the task's **Settings** tab, tick *If the task fails, restart every 1
minute*, and untick *Stop the task if it runs longer than…* — otherwise
Windows kills your wall after three days.

## 5. Suppress the interruptions

```
:: no notification toasts over the photos
reg add "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications" /v ToastEnabled /t REG_DWORD /d 0 /f

:: no touch keyboard popping up when the page is tapped
reg add "HKCU\SOFTWARE\Microsoft\TabletTip\1.7" /v EnableDesktopModeAutoInvoke /t REG_DWORD /d 0 /f
```

For Windows Update, set **active hours** to cover your waking day
(Settings → Windows Update → Advanced options) so restarts land overnight.
Don't disable updates outright on an internet-connected machine.

## 6. Verify it survives a reboot

The only test that matters:

1. Reboot the Cubi.
2. Do not touch anything.
3. After ~60 seconds the wall should be showing photos.

Then leave it a full day and check it hasn't blanked, logged out, or reset to
a first-run screen.

---

## Known gaps

- **HTTPS.** Everything here is plain HTTP on the LAN, which is why the wake
  lock doesn't work. If you later put a reverse proxy with a certificate in
  front of Kiosk, `KIOSK_DISABLE_SCREENSAVER: "true"` becomes useful and step 3
  becomes belt-and-braces.
- **Touch handoff.** Tapping currently does whatever Kiosk's own navigation
  does. The layered "tap for context, tap again to dig in" behaviour is
  Phase 6 — the wrapper page, not a Kiosk setting.
- **Recovery.** If the Synology is down or Kiosk is restarting, Edge shows a
  connection error full-screen. A small retry page pointed at Kiosk would be
  friendlier, and belongs with the Phase 6 wrapper.
