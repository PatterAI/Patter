# Hackathon Day Strategy — Sunday April 12, 2026

## Context

- **Sprint window:** 3 hours (11:30 AM - 2:30 PM)
- **What we are building:** Masterminding LEO — an adaptive learning platform with AI content generation, Bayesian mastery tracking, and teacher analytics
- **Two tracks:** Google AI Studio (Gemini) and FastShot.ai (Mobile)
- **Submission:** 2:30 PM sharp
- **Semi-finals:** Async panel review
- **Finals:** Top 5 teams, 5-min pitch + 5-min Q&A

---

## Strategy: What to Build on Hackathon Day

The prerequisite prototypes (Google AI Studio + FastShot) are warmups. On hackathon day, build a more complete, polished, demo-ready version of LEO that showcases three pillars:

1. **Adaptive learning** — difficulty adjusts based on student performance
2. **AI content generation** — lessons and quizzes generated from any topic via Gemini
3. **Teacher analytics** — mastery heatmap, student progress dashboard

Focus on demo impact over feature completeness. A smooth 5-minute demo of three working features beats ten half-broken features.

---

## Minute-by-Minute Sprint Plan

| Time | Milestone | What to Do |
|------|-----------|------------|
| 11:30 - 11:45 | **Setup** (15 min) | Set up project in AI Studio / FastShot. Import any code prepared beforehand. Agree on final scope. Assign roles. |
| 11:45 - 12:15 | **Core MVP** (30 min) | Build the student-facing adaptive quiz flow: topic input, lesson generation, quiz with difficulty levels, mastery score. This is the hero demo. |
| 12:15 - 12:45 | **Teacher View** (30 min) | Add a simple teacher dashboard: class overview, mastery heatmap placeholder, student list. Can use simulated data. |
| 12:45 - 1:15 | **AI Magic** (30 min) | Polish AI content generation: make lessons rich, quizzes varied, add difficulty labels. This is the "wow" factor for judges. |
| 1:15 - 1:45 | **Polish and Deploy** (30 min) | Fix bugs, improve UI, deploy to Cloud Run. Ensure demo URL works publicly. |
| 1:45 - 2:15 | **Demo Prep** (30 min) | Record 1-min demo video. Write one-pager if not done. Prepare YouTube upload for social traction points. |
| 2:15 - 2:30 | **Submit** (15 min) | Submit all required materials. Double-check everything. Breathe. |

---

## Checkpoint Discipline

At each 30-minute mark, ask one question:

> "Can we demo what we have right now?"

- **If yes:** Continue adding the next feature.
- **If no:** Stop feature work immediately. Fix what is broken until the app is demoable again.

Never leave the app in a non-demoable state. A working subset always beats a broken superset.

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| AI Studio goes down | FastShot is the backup. Both tracks accept submissions independently. |
| Prototype crashes during demo | Record the demo video early (at 1:45 PM). Live demo is optional if the video is strong. |
| Feature creep | Scope is locked at 11:45 AM. No new features after 1:15 PM — only polish and bug fixes. |
| Deployment fails | Deploy early (by 1:00 PM) and iterate. Never save deployment for the end. |
| Team disagreements | Clear roles from minute 1: one person decides scope, one codes, one designs, one prepares pitch. |
| API rate limits | Have fallback static responses ready for key demo moments. Cache successful API responses. |
| Slow internet at venue | Pre-download all dependencies. Have a mobile hotspot as backup. |

---

## Social Traction Strategy

Judging includes social engagement on demo videos (likes, shares, views counted for 2 weeks post-event).

### Execution Plan

1. **Upload** a 1-min demo video to YouTube as UNLISTED initially
2. **Switch to PUBLIC** at submission time (2:30 PM)
3. **Post immediately** after the event on:
   - LinkedIn (professional angle: "adaptive learning powered by Gemini")
   - Twitter/X (thread format: problem, solution, demo link)
4. **Ask friends and network** to like and share within the first 24 hours
5. **Hashtags:** #StanfordHackathon #DeepMind #GeminiAI #EdTech #AdaptiveLearning

### Content Templates

**LinkedIn post hook:** "We just built an adaptive learning platform at the Stanford x DeepMind hackathon — here is what LEO does in 60 seconds."

**Twitter/X thread opener:** "Thread: We built LEO at the Stanford hackathon today. It generates personalized lessons on any topic and adapts quiz difficulty in real time using Bayesian mastery tracking. Here is a 1-min demo..."

---

## What to Prepare BEFORE Hackathon Day

- [ ] One-pager draft (see 08-one-pager-draft.md)
- [ ] Pitch outline practiced (see 07-pitch-outline.md)
- [ ] Video scripts ready (see 09-video-scripts.md)
- [ ] LEO brand assets ready (logo, color scheme: deep blue `#1e3a5f` + amber `#f59e0b`)
- [ ] Team roles assigned and agreed upon
- [ ] GitHub repo initialized (for Google AI Studio track code repo requirement)
- [ ] All dependencies pre-installed locally
- [ ] Cloud Run project configured and tested with a hello-world deploy
- [ ] Demo script rehearsed at least once end-to-end
- [ ] Backup static data prepared in case APIs are slow or down

---

## Day-Of Packing List

- Laptop + charger
- Mobile hotspot (backup internet)
- Phone with Expo Go installed (for FastShot testing)
- Printed one-pager (5 copies for judges)
- Water + snacks (3 hours is a long sprint)
- Headphones (for recording demo video without background noise)
