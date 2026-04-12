# Platform Research: FastShot.ai + Playcast

Stanford x DeepMind Hackathon | April 12, 2026

---

## FastShot.ai -- What It Is

FastShot.ai is a **Y Combinator-backed (Fall 2025) AI-powered mobile app builder** that converts natural language descriptions into fully functional React Native + Expo mobile apps. It is often described as "Lovable for mobile apps." No coding is required.

- **Founded by:** Dmitry Fatkhi (ex-Google, 10+ yrs AI/ML) and Elvira Dzhuraeva (ex-Google GenAI, Kubeflow 1.0 launch lead)
- **Team size:** 3
- **Positioning:** End-to-end mobile development platform covering Ideation, Design, Development, and Deployment
- **Claims:** 10x faster than traditional development, 100x cheaper than agencies ($60K-$120K typical MVPs)

---

## FastShot.ai -- How It Works

### Building an App

1. **Go to [fastshot.ai](https://fastshot.ai/)** and open the editor
2. **Describe your app** in the chat editor using plain English. Be specific: name screens, layout structures, data models, user interactions, and design direction (colors, fonts, spacing)
3. **AI clarifies requirements** -- it may ask follow-up questions before building
4. **AI generates the app** -- takes 5-7 minutes for the first version. A live preview appears in the right panel
5. **Iterate** -- send follow-up messages describing changes. Each message triggers targeted updates, not full rebuilds. Make one change at a time for best results
6. **Test and deploy** when satisfied

### Tech Stack (generated apps)

| Layer | Technology |
|-------|-----------|
| Framework | React Native |
| Build/Deploy | Expo + Expo Router (file-based navigation) |
| Language | TypeScript |
| State management | Zustand |
| Backend (optional) | Supabase (auth, database, storage) |
| Monetization (optional) | RevenueCat or Adapty |

### AI Workflow Pipeline

FastShot uses a multi-agent architecture with 6 stages:

1. **Describe** -- user provides natural language requirements
2. **Plan** -- AI architects app structure (screens, navigation, data models, design)
3. **Build** -- AI generates production-ready React Native + Expo code
4. **Preview** -- live browser preview + QR code for Expo Go
5. **Iterate** -- targeted code updates from follow-up messages
6. **Deploy** -- distribution via Expo EAS builds to App Store / Google Play

---

## FastShot.ai -- Preview and Testing

### Browser Preview

- Live preview in the right panel of the editor
- Updates automatically as changes are made
- **Limitation:** Browser preview is approximate. It cannot simulate native device features: camera, biometrics, NFC, Bluetooth, push notifications, native date pickers
- Platform-specific UI is approximate in browser but accurate on device

### Physical Device Testing (Expo Go)

1. Install **Expo Go** on your phone ([iOS](https://apps.apple.com/us/app/expo-go/id982107779) / [Android](https://play.google.com/store/apps/details?id=host.exp.exponent&hl=en_US))
2. Find the **QR code tab** in the preview panel of the FastShot editor
3. Scan with your phone camera (iOS) or the Expo Go app (Android)
4. App loads and runs on your phone with live updates

**Requirement:** Phone and laptop must be on the **same Wi-Fi network** for live preview.

### Preview States

Loading, Building, Ready, Coding, Error. You can restart Metro or reset cache via AI instruction.

---

## FastShot.ai -- Getting the Shareable Prototype Link

For the hackathon submission, you need the FastShot project link. The process:

1. In the FastShot editor, click the **"Share" button**
2. This generates a shareable URL to your project
3. Paste this URL into the Google Form submission

Note: The exact sharing mechanism is not detailed in their docs beyond the Share button. The prerequisite doc (`03-fastshot-prereq.md`) confirms: *"Use the Share button in FastShot to get the link."*

---

## FastShot.ai -- Backend and API Capabilities

### Supabase Integration (Optional)

When your app needs backend functionality, FastShot auto-prompts you to connect Supabase:

- **Authentication:** email/password, Google sign-in, Apple sign-in (automatic session management)
- **Database:** PostgreSQL with real-time subscriptions
- **Storage:** File/image uploads, public URLs, access control (10 MB file limit, `image/*` MIME types default)

If you decline Supabase, FastShot falls back to **AsyncStorage** (local persistence) + **Zustand** (state management). No cloud features or auth in this mode.

### External API Calls

The docs do not explicitly document whether FastShot apps can make external HTTP calls (e.g., `fetch()` to a custom backend). However, since the generated code is standard React Native + TypeScript, it is technically possible to:

- Use `fetch()` or `axios` for HTTP requests
- The generated code is real React Native -- you can ask FastShot to add API calls in your prompts
- GitHub integration allows exporting the code for manual editing

**For the hackathon:** The FastShot mobile app for ShipCall needs to POST to the FastAPI backend (`/api/call-me`). This should work by prompting FastShot to include a REST API call. If it doesn't, the phone call (triggered manually or via the backend directly) is the real product, not the mobile app.

### GitHub Integration

- Code is pushed to GitHub automatically when you deploy
- You can clone the repo and work on it locally
- Standard React Native + Expo patterns
- You own the repository after export

---

## FastShot.ai -- Constraints and Gotchas

| Constraint | Details |
|-----------|---------|
| **Initial build time** | 5-7 minutes for first version |
| **Browser preview** | Approximate only; native features don't work |
| **Same Wi-Fi required** | Phone and laptop must be on same network for Expo Go |
| **Iteration strategy** | One change at a time for best results; bundled requests cause issues |
| **Supabase file size** | 10 MB per file maximum |
| **No explicit pricing docs** | Free tier details not publicly documented |
| **Complexity tiers** | The prerequisite doc mentions Tier 1 (simple) and Tier 2 (medium). The LEO Micro-Quiz is Tier 2 (multiple screens, state management, dynamic content). Expect 30-40 min for a Tier 2 app. |
| **Prompting quality** | Vague prompts produce poor results. Be specific about screens, data models, interactions, and visual design |

---

## FastShot.ai -- Hackathon-Specific Notes

### What the FastShot Track Requires

| Requirement | Details |
|-------------|---------|
| One-pager | Written project summary |
| Hosted prototype | FastShot share link (use the Share button) |
| Team intro video | 2-minute video introducing the team |
| Demo video | 1-minute walkthrough (must be public on YouTube) |

### For ShipCall

The FastShot mobile app serves as a **companion/prop**, not the core product. Keep it to 1-2 screens:

1. **Event Feed** -- recent events with status icons
2. **Call Me Now button** -- POST to backend, phone rings

If FastShot fails to build or the app is buggy, the phone call demo still works independently. The mobile app adds polish but is not critical path.

### Gemini Credits

The hackathon offers **Gemini build credits** through FastShot. This may mean FastShot has Gemini integration for AI features within apps, but the exact mechanism is not documented. The GCP credits prize pool is $60K (in-person) and $10K (online).

---

## Playcast -- Research Findings

### What Playcast Is NOT (for this hackathon)

There is **no tool called "Playcast"** that is specific to this hackathon's submission requirements. Extensive search found:

- **Playcast.io** -- a peer-to-peer game streaming platform (OBS-based, for gamers). Not a demo recording tool.
- **Playcast Media Systems** -- a cloud gaming company acquired by EA in 2018. Defunct.
- **Playcast (Android)** -- a gameplay recording app from 2016.

### No Mention in Hackathon Materials

- The Luma event page does not mention "Playcast"
- The hackathon overview doc (`01-hackathon-overview.md`) does not mention "Playcast"
- The submission prep doc (`05-submission-prep.md`) does not mention "Playcast"
- Grep across all `.hackathon/` files returns zero matches for "Playcast"

### What the Hackathon Actually Requires for Video

The hackathon requires:

1. **2-minute team intro video** -- direct to camera, introducing team and idea
2. **1-minute demo video** -- screen recording walkthrough of the product
3. **Both must be public on YouTube** -- social engagement (likes, shares, views) counts toward judging for 2 weeks post-event

### How to Record Demo Videos

| Method | Platform | Details |
|--------|----------|---------|
| Phone screen recording | iOS | Swipe down from top-right, tap screen record button |
| Phone screen recording | Android | Swipe down notification shade, tap "Screen Record" |
| Desktop screen recording | macOS | Built-in screen recorder, OBS Studio, or Loom |
| Desktop screen recording | Any | OBS Studio (free, open source) |

### Recommended Workflow for Demo Video

1. Record with phone screen recording (for FastShot/Expo Go demos) or OBS/macOS screen recorder (for web demos)
2. Upload to YouTube as **UNLISTED** initially
3. Switch to **PUBLIC** at submission time (2:30 PM)
4. Include clear title: "ShipCall -- Your Code Has a Voice | Stanford x DeepMind Hackathon 2026"
5. Keep under 60 seconds

---

## Conclusion: "Playcast" Is Likely a Misidentification

The term "Playcast" does not appear anywhere in the hackathon materials, event pages, or submission requirements. It is possible that:

1. "Playcast" was confused with a different tool (perhaps someone mentioned a screen recording approach casually)
2. It refers to Playcast.io's game streaming, which is irrelevant to this hackathon
3. It was a misremembered name for another demo recording service (Loom, Vidcast, etc.)

**Bottom line:** The hackathon requires YouTube videos recorded via standard screen recording tools. No special "Playcast" tool is needed.

---

## Updated Build Strategy Recommendations

### FastShot.ai Strategy

1. **Keep the FastShot app minimal.** 1-2 screens maximum: event feed + Call Me Now button
2. **Write a specific prompt.** Name every screen, describe the data, specify colors (`#1e3a5f` deep blue + `#f59e0b` amber from LEO branding)
3. **Prompt FastShot to include an API call.** The Call Me Now button needs to POST to `{backend_url}/api/call-me`
4. **Test on phone via Expo Go.** Same Wi-Fi required
5. **Get the share link** via the Share button in the editor
6. **If FastShot fails:** The phone call demo works without the mobile app. Don't waste more than 25 minutes on FastShot

### Video Recording Strategy

1. **Record both videos early** (at 1:45 PM, not 2:25 PM)
2. **ShipCall demo:** Record phone screen showing the FastShot app, then switch to speakerphone for the AI call
3. **VoiceScope demo:** Record desktop showing the web UI upload, then switch to speakerphone
4. **Upload to YouTube as UNLISTED** immediately
5. **Switch to PUBLIC at 2:30 PM** and share on LinkedIn + Twitter for social traction points

### Critical Path

```
0:00  -- Patter outbound call working (MUST HAVE by 0:25)
0:25  -- Tool handlers wired up
0:50  -- FastShot app + FastAPI endpoint
1:00  -- CHECKPOINT: ShipCall demoable?
1:15  -- Gemini 3 vision pipeline
1:30  -- VoiceScope callback + web UI
1:45  -- Record demo videos (BOTH)
2:05  -- Write one-pagers + polish
2:25  -- Submit + go public on YouTube
```

The phone ringing is the product. Everything else is enhancement.
