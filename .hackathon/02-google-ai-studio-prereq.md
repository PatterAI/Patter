# Google AI Studio Prerequisite -- Stanford x DeepMind Hackathon

**Deadline: Friday, April 10, 2026 -- 10:00 PM PST**

---

## Overview

Build a working prototype in **Google AI Studio** using **Gemini 3**, deploy it to **Cloud Run**, and submit the deployed URL via Google Form. The purpose of this prerequisite is to get familiar with the Google AI Studio platform and to ensure your GCP billing is enabled before the hackathon on April 12.

---

## Prerequisites Checklist

- [ ] Google account
- [ ] Access to Google AI Studio at [aistudio.google.com](https://aistudio.google.com/)
- [ ] Google Cloud Project created at [console.cloud.google.com](https://console.cloud.google.com)
- [ ] Billing account enabled (free trial codelab: [cloud-codelab-credits](https://codelabs.developers.google.com/codelabs/cloud-codelab-credits#0))
- [ ] No local tooling needed -- everything happens in the browser

---

## Step-by-Step Instructions

### Step 1: Access Google AI Studio

1. Go to [aistudio.google.com](https://aistudio.google.com/)
2. Sign in with your Google account
3. Click **"Build"** in the left-side menu

---

### Step 2: Build the "LEO Study Buddy" Prototype

1. In Build mode, paste the prompt below into the input box
2. Select **"Gemini 3 Flash Preview"** as the model
3. Click **"Build"**

#### Ready-to-Paste Prompt

```
Build me an educational app called "LEO Study Buddy".

The app should:
1. Ask the student to enter a topic they want to study
2. Generate a short adaptive lesson with 3 sections:
   - Overview (2-3 sentences explaining the topic)
   - Key Concepts (3-5 bullet points with the most important ideas)
   - Worked Example (one concrete example showing the concept in action)
3. After the lesson, present a 5-question multiple choice quiz with increasing difficulty:
   - Questions 1-2: Easy (basic recall)
   - Questions 3-4: Medium (application)
   - Question 5: Hard (analysis/synthesis)
4. Score the student and show a mastery level:
   - 5/5: "Master" with a gold badge
   - 3-4/5: "Proficient" with a silver badge
   - 1-2/5: "Learning" with encouragement to retry
   - 0/5: "Getting Started" with a suggestion to review
5. Let the student try another topic or retry the same one

Use gemini-3-flash-preview to generate all content.
Make it visually clean with a modern educational design.
Use a color scheme of deep blue (#1e3a5f) and warm amber (#f59e0b).
```

---

### Step 3: Test the App

1. The preview loads automatically in the right panel
2. Test the full flow: **enter a topic** --> **read the lesson** --> **take the quiz** --> **see your score**
3. Iterate if needed using AI-suggested enhancements or custom requests in the Build input

---

### Step 4: Deploy to Cloud Run

1. Click the **"Deploy App"** button (top right)
2. Select or import your GCP project
3. Ensure billing verification passes
4. Click **"Deploy app"**
5. Wait a few minutes for the deployment to complete
6. Copy the **App URL** when it is ready

---

### Step 5: Verify

1. Open the App URL in a new browser tab
2. Run through the full flow to confirm everything works end-to-end
3. Note: Cloud Run offers **2 million free requests/month** -- no cost concerns for this exercise

---

## Submit via Google Form

| Detail       | Value |
|--------------|-------|
| **Form URL** | [https://forms.gle/7Q9XHZGNrM61SCTq7](https://forms.gle/7Q9XHZGNrM61SCTq7) |
| **Required** | Your deployed App URL and any screenshots |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| **Can't see Build mode?** | Make sure you are at [aistudio.google.com](https://aistudio.google.com/) (also accessible via [ai.dev](https://ai.dev)) |
| **Billing not enabled?** | Follow the free trial codelab: [cloud-codelab-credits](https://codelabs.developers.google.com/codelabs/cloud-codelab-credits#0) |
| **Deploy fails?** | Check that billing is active on the selected GCP project |
| **App not loading?** | Wait 2-3 minutes after deployment for the container to start |

---

## Useful Links

| Resource | URL |
|----------|-----|
| Google AI Studio | https://aistudio.google.com/ |
| Google Cloud Console | https://console.cloud.google.com |
| Cloud Run Console | https://console.cloud.google.com/run |
| Free Trial Signup | https://console.cloud.google.com/freetrial/ |
| AI Studio Build Mode Docs | https://ai.google.dev/gemini-api/docs/aistudio-build-mode |
| Deploy from AI Studio Codelab | https://codelabs.developers.google.com/deploy-from-aistudio-to-run |
| Getting Started with Gemini 3 Tutorial | https://cloud.google.com/blog/topics/developers-practitioners/getting-started-with-gemini-3-deploy-your-first-gemini-3-app-to-google-cloud-run |

---

## Time Estimate

| Phase | Duration |
|-------|----------|
| Setup (account + billing) | ~5 min |
| Build the prototype | ~5 min |
| Test + Deploy | ~5 min |
| Submit the form | ~5 min |
| **Total** | **~15-20 minutes** |
