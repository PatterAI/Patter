# FastShot.ai Prerequisite Guide

**Stanford x DeepMind Hackathon -- April 12, 2026**
**Deadline: Friday, April 10, 10:00 PM PST**

---

## Overview

Build a mobile app using [FastShot.ai](https://fastshot.ai/), preview it on your phone via Expo Go, record a short demo video, and submit everything through a Google Form. The purpose of this assignment is to get comfortable with FastShot before the hackathon so you can move fast on event day.

---

## Prerequisites Checklist

- [ ] FastShot.ai account (register at [fastshot.ai](https://fastshot.ai/))
- [ ] Expo Go app installed on your phone
  - iOS: [App Store](https://apps.apple.com/us/app/expo-go/id982107779)
  - Android: [Google Play](https://play.google.com/store/apps/details?id=host.exp.exponent&hl=en_US)
- [ ] Phone and laptop connected to the **same Wi-Fi network**
- [ ] Screen recording capability on your phone (built-in on both iOS and Android)

---

## Step-by-Step Instructions

### Step 1: Register at FastShot.ai

1. Go to [fastshot.ai](https://fastshot.ai/)
2. Create an account and sign in

---

### Step 2: Build the "LEO Micro-Quiz" App

1. Create a **new project** in FastShot
2. Paste the following prompt into the project description

**Ready-to-Paste Prompt:**

```
Build a mobile quiz app called "LEO Micro-Quiz" for students.

The app should have these screens:

1. Home Screen:
   - App title "LEO Micro-Quiz" with a graduation cap icon
   - A text input where the student types a subject/topic
   - A "Start Quiz" button
   - Color scheme: deep blue (#1e3a5f) background with white text and amber (#f59e0b) accent buttons

2. Quiz Screen:
   - Show one question at a time with 4 multiple choice options
   - 5 questions total with increasing difficulty:
     * Questions 1-2 labeled "Easy" with a green badge
     * Questions 3-4 labeled "Medium" with a yellow badge
     * Question 5 labeled "Hard" with a red badge
   - Progress bar at the top showing question number (e.g., "3 of 5")
   - When user taps an answer, highlight green if correct, red if wrong
   - Show the correct answer if they got it wrong
   - "Next" button to proceed

3. Results Screen:
   - Big mastery score as percentage
   - Mastery level badge:
     * 100%: "Master" (gold star)
     * 60-80%: "Proficient" (silver star)
     * 20-40%: "Learning" (bronze star)
     * 0%: "Getting Started" (encouragement message)
   - Summary of which questions were right/wrong
   - "Try Another Topic" button to go back to home
   - "Retry" button to retake same quiz

Generate sample quiz questions for any topic the user enters.
Make the UI clean, modern, and mobile-friendly with smooth transitions.
```

---

### Step 3: Test in FastShot Editor

1. Wait for the app to generate
2. Test the full flow in the editor preview:
   - Enter a topic on the home screen
   - Answer all 5 questions
   - Check the results screen
3. If anything looks off, **describe the changes in plain English** and let FastShot iterate

---

### Step 4: Preview on Phone via Expo Go

1. In the FastShot editor, find the **"Preview on device"** option
2. Open **Expo Go** on your phone
3. Scan the QR code:
   - **iPhone:** Use the Camera app or the Expo Go built-in scanner
   - **Android:** Use the Expo Go built-in scanner
4. The app should load and run on your phone

**Troubleshooting:**
If the app does not load, verify that your phone and laptop are on the **same Wi-Fi network**, then re-scan the QR code.

---

### Step 5: Record Demo Video (15-45 seconds)

Start screen recording on your phone:

| Platform | How to Record | Tutorial |
|----------|---------------|----------|
| **iPhone** | Swipe down from top-right corner, tap the screen record button | [Watch tutorial](https://youtube.com/shorts/kIf3VXyj7IA?si=Sw5LOTa2QTzEeTIX) |
| **Android** | Swipe down the notification shade, tap "Screen Record" | [Watch tutorial](https://youtube.com/shorts/hv4l0D5ILxI?si=t_QIJEuEQ3fNsTtL) |

**Demo Video Shot List (aim for ~30 seconds):**

| Timestamp | What to Show |
|-----------|--------------|
| 0-5s | App opening on the home screen |
| 5-10s | Type a topic (e.g., "Photosynthesis") and tap "Start Quiz" |
| 10-20s | Answer 2-3 questions, showing the difficulty progression |
| 20-25s | Results screen with mastery score |
| 25-30s | Tap "Try Another Topic" briefly to show the flow |

---

### Step 6: Submit via Google Form

**Form URL:** [https://forms.gle/RsaT48aagx2fBzB7A](https://forms.gle/RsaT48aagx2fBzB7A)

Have the following ready before you open the form:

| Field | Value |
|-------|-------|
| **App name** | LEO Micro-Quiz |
| **Description** | A mobile adaptive quiz app where students enter any topic and take difficulty-stratified quizzes with mastery scoring -- a prototype of the LEO adaptive learning platform. |
| **FastShot project link** | Use the **Share** button in FastShot to get the link |
| **Demo video** | Upload the file or paste a link (Google Drive, YouTube, etc.) |

---

## App Complexity Note

This is a **Tier 2 (Medium Complexity)** app per FastShot's guidelines. It involves multiple screens, state management (tracking scores), and dynamic content generation. You should be able to build it in a single **30-40 minute** pass.

**Fallback plan:** If you run into trouble, simplify to a **Tier 1** version -- remove the difficulty progression and just show 5 straightforward questions with a final score.

---

## Useful Links

| Resource | URL |
|----------|-----|
| FastShot.ai | [https://fastshot.ai/](https://fastshot.ai/) |
| Expo Go (iOS) | [https://apps.apple.com/us/app/expo-go/id982107779](https://apps.apple.com/us/app/expo-go/id982107779) |
| Expo Go (Android) | [https://play.google.com/store/apps/details?id=host.exp.exponent&hl=en_US](https://play.google.com/store/apps/details?id=host.exp.exponent&hl=en_US) |
| Assignment page | [https://www.notion.so/Fastshot-pre-Hackathon-Home-Assignment-aeca6675ecaa46c2bddf295643bcbd75](https://www.notion.so/Fastshot-pre-Hackathon-Home-Assignment-aeca6675ecaa46c2bddf295643bcbd75) |
| Submission form | [https://forms.gle/RsaT48aagx2fBzB7A](https://forms.gle/RsaT48aagx2fBzB7A) |

---

## Time Estimate

| Phase | Duration |
|-------|----------|
| Account setup + Expo Go install | ~5 min |
| Build + iterate in FastShot | ~30-40 min |
| Phone preview via Expo Go | ~5 min |
| Record demo video | ~5 min |
| Submit via Google Form | ~5 min |
| **Total** | **~45-60 min** |
