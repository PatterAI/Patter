# Alternative LEO Prototype Ideas -- Stanford x DeepMind Hackathon

These are backup prototype ideas themed around the LEO adaptive learning platform. Use them if the primary choices -- **LEO Study Buddy** (Google AI Studio track) and **LEO Micro-Quiz** (FastShot.ai track) -- hit blockers or feel too risky on hackathon day.

Each idea includes a ready-to-paste prompt so you can spin up a working prototype in minutes.

---

## Google AI Studio Track (Gemini 3) -- 3 Alternative Ideas

---

### Idea 1: "LEO Lesson Generator"

**Concept:** A teacher enters a topic, grade level, and learning objective. Gemini generates a complete multi-section lesson plan with an overview, key concepts, worked examples, common misconceptions, and recall questions -- everything needed to walk into a classroom and teach.

**LEO Feature Showcased:** AI content generation pipeline (the same engine that powers LEO's automated notes, quizzes, and topic breakdowns).

**User Flow:**
1. Enter a topic (e.g., "Photosynthesis")
2. Select grade level (e.g., "Grade 8")
3. Type a learning objective (e.g., "Students will explain the role of chlorophyll")
4. Click "Generate Lesson"
5. View the structured lesson plan with all sections
6. Download as PDF or share via link

**Ready-to-Paste Prompt:**

```
Build me an educational app called "LEO Lesson Generator".

The app should:
1. Show a clean input form where a teacher enters:
   - Topic name (text field, required)
   - Grade level (dropdown: Grades 1-12, Undergraduate, Graduate)
   - Learning objective (text area, required, e.g. "Students will be able to explain...")
2. When the teacher clicks "Generate Lesson", use gemini-3-flash-preview to produce
   a structured lesson plan with these exact sections:
   - OVERVIEW: A 3-4 sentence summary of the topic at the chosen grade level
   - KEY CONCEPTS: 4-6 bullet points covering the most important ideas
   - WORKED EXAMPLE: One detailed, step-by-step example showing the concept in action
   - COMMON MISCONCEPTIONS: 3 mistakes students frequently make, with corrections
   - RECALL QUESTIONS: 5 open-ended questions that test understanding (not multiple choice)
3. Display the generated lesson in a well-formatted card layout with clear section headers.
4. Include a "Regenerate" button to get a different version of the same lesson.
5. Include a "New Lesson" button to start over with a fresh topic.
6. At the bottom, show a small "Lesson Quality" indicator that rates
   the generated content on alignment with the stated learning objective (High / Medium / Low).

Design: Professional and teacher-friendly. Use a deep navy (#1a2744) primary color,
a fresh green (#22c55e) for success states, and clean white cards with subtle shadows.
Use a sans-serif font with generous spacing for readability.
```

---

### Idea 2: "LEO Misconception Detector"

**Concept:** A student writes a free-form paragraph explaining their understanding of a topic. Gemini analyzes the explanation for misconceptions, highlights exactly where the reasoning goes wrong, and provides gentle, targeted corrections. Think of it as a patient tutor reading over the student's shoulder.

**LEO Feature Showcased:** Adaptive feedback and misconception detection (a core piece of LEO's Bayesian mastery tracking -- the system identifies not just what students don't know, but what they incorrectly believe).

**User Flow:**
1. Select a subject area (e.g., "Biology", "Physics", "History")
2. Enter a topic within that subject (e.g., "Natural Selection")
3. Write a paragraph explaining what the student thinks they know
4. Click "Analyze My Understanding"
5. See highlighted misconceptions with inline corrections
6. Read a summary with a "confidence score" and suggestions for further study

**Ready-to-Paste Prompt:**

```
Build me an educational app called "LEO Misconception Detector".

The app should:
1. Display a subject picker (dropdown with: Biology, Chemistry, Physics,
   Mathematics, History, Computer Science, Economics, Literature).
2. Below the picker, show a text field for the specific topic
   (e.g., "Photosynthesis", "Newton's Third Law").
3. Below that, show a large text area (at least 6 lines) with the prompt:
   "Explain this topic in your own words. Write as if you're teaching
   it to a friend. Don't worry about being perfect -- that's what I'm here for."
4. When the student clicks "Analyze My Understanding", use gemini-3-flash-preview to:
   a. Identify every misconception or factual error in the student's explanation.
   b. For each misconception, provide:
      - The exact quote from the student's text that contains the error
      - What is wrong and why
      - The correct explanation, written in a supportive, non-judgmental tone
   c. Identify parts of the explanation that are correct and praise them specifically.
   d. Generate a "Comprehension Score" from 0-100 based on accuracy and completeness.
   e. Suggest 2-3 specific concepts the student should review next.
5. Display results in a split view:
   - Left panel: the student's original text with misconceptions highlighted in
     soft red (#fecaca) and correct parts highlighted in soft green (#dcfce7)
   - Right panel: detailed feedback cards for each misconception
6. Include a "Try Again" button that lets the student revise and resubmit.

Design: Warm and encouraging -- this is not a grading tool, it's a learning companion.
Use a calming blue (#3b82f6) primary color, soft backgrounds (#f8fafc), and rounded
card corners. Tone of all feedback must be supportive and constructive.
```

---

### Idea 3: "LEO Teacher Dashboard Simulator"

**Concept:** A demo analytics dashboard for a simulated classroom of 25 students. Gemini generates realistic sample data -- mastery heatmaps, content effectiveness charts, grade distributions, and at-risk student alerts. Teachers can then ask natural-language questions about the data ("Which students are struggling with photosynthesis?", "What topic should I reteach?") and get AI-powered insights.

**LEO Feature Showcased:** Teacher analytics and data-driven insights (the real LEO platform includes mastery heatmaps, event tracking, and Celery-powered aggregation pipelines for teacher dashboards).

**User Flow:**
1. Land on a dashboard showing a simulated class of 25 students
2. Explore a mastery heatmap (students vs. topics, color-coded by proficiency)
3. View content effectiveness charts (which materials led to the most learning gains)
4. Ask a natural-language question about the data in a chat input
5. Get an AI-generated insight with specific student names and recommendations

**Ready-to-Paste Prompt:**

```
Build me an educational analytics app called "LEO Teacher Dashboard Simulator".

The app should:
1. On load, use gemini-3-flash-preview to generate realistic simulated data for
   a class called "Biology 101" with 25 students (use realistic first names).
   Each student has mastery scores (0-100) across 6 topics:
   Cell Structure, Photosynthesis, Genetics, Evolution, Ecology, Human Anatomy.
   Make the data realistic -- some students excel, some struggle, most are in the middle.
   Include 3-4 students who are clearly "at risk" (below 40 in multiple topics).
2. Display a MASTERY HEATMAP: a grid with students on the Y-axis and topics on the
   X-axis. Color-code cells: red (0-39), orange (40-59), yellow (60-74),
   green (75-89), dark green (90-100). Show the numeric score in each cell.
3. Display a CLASS AVERAGES bar chart showing the average mastery per topic.
4. Display an AT-RISK ALERTS section listing students below 40 in 2+ topics,
   with their names and the specific topics they need help with.
5. Include a NATURAL LANGUAGE QUERY box at the bottom where the teacher can type
   questions like:
   - "Which students are struggling with Genetics?"
   - "What's the hardest topic for the class?"
   - "Suggest a reteaching plan for the bottom 5 students"
   Gemini should answer using the simulated data, referencing specific students
   and scores.
6. Include a "Regenerate Class" button that creates an entirely new set of
   simulated students and scores.

Design: Dashboard-style with a dark sidebar (#1e293b), white content area, and
data-visualization-forward layout. Use a professional color scheme appropriate for
a teacher analytics tool. Charts should be clear and labeled.
```

---

## FastShot.ai Track (Mobile App) -- 3 Alternative Ideas

---

### Idea 1: "LEO Flashcard Flow"

**Concept:** A spaced-repetition flashcard app. The student enters a topic, and AI generates a deck of flashcards. Cards answered incorrectly come back sooner; cards answered correctly space out further. A streak counter and simple mastery bar keep motivation high.

**Complexity:** Tier 1 (simpler than the primary LEO Micro-Quiz choice -- fewer moving parts, faster to build).

**LEO Feature Showcased:** Spaced retrieval and mastery tracking (LEO's adaptive engine uses Bayesian knowledge tracing to determine when to resurface material -- this is the simplified mobile version of that loop).

**User Flow:**
1. Enter a topic (e.g., "Spanish Vocabulary -- Food")
2. View the front of a flashcard (question or term)
3. Tap to flip and see the answer
4. Rate difficulty: Easy / Medium / Hard
5. Cards rated Hard come back after 1 card; Medium after 3; Easy after 7
6. See a progress bar and streak counter at the top

**Ready-to-Paste Prompt:**

```
Build me a mobile flashcard app called "LEO Flashcard Flow".

The app should:
1. Show a welcome screen with a text input for the study topic and
   a "Generate Deck" button.
2. When the user enters a topic, use AI to generate 10 flashcards.
   Each card has a FRONT (question, term, or concept prompt) and
   a BACK (answer, definition, or explanation).
3. Display cards one at a time in a swipeable card interface.
   The front shows first. The user taps to flip and reveal the back.
4. After seeing the answer, the user rates the card:
   - "Easy" (green button) -- card moves to the end of the queue, spaced 7 cards out
   - "Medium" (yellow button) -- card re-enters the queue 3 cards later
   - "Hard" (red button) -- card comes back after just 1 more card
5. Show a progress bar at the top: "Card 3 of 10" and a streak counter
   ("Current streak: 5 correct").
6. When all cards are rated Easy at least once, show a completion screen
   with the message "Deck Mastered!" and the total streak count.
7. Include a "New Topic" button to generate a fresh deck.

Design: Clean mobile-first layout. Large, readable card text.
Primary color: deep blue (#1e3a5f). Accent: warm amber (#f59e0b).
Smooth flip animation on the cards. Rounded corners and generous padding.
```

---

### Idea 2: "LEO Podcast Player"

**Concept:** A mobile app that generates short 2-3 minute educational "podcast episodes" on any topic. The student enters a subject, and the AI produces a conversational script formatted with speaker labels, timestamps, and a natural back-and-forth between a "host" and an "expert." After the episode, a quick 3-question comprehension check reinforces the material.

**Complexity:** Tier 2 (moderately complex -- requires text generation, structured formatting, and a quiz component).

**LEO Feature Showcased:** Multi-modal content generation (the real LEO platform generates notes, podcasts, mindmaps, quizzes, and video content -- this prototype demonstrates the podcast pipeline in isolation).

**User Flow:**
1. Enter a topic (e.g., "How Black Holes Form")
2. Tap "Generate Episode"
3. Read the podcast-style script with speaker labels and section timestamps
4. Scroll through the conversation between "Host" and "Expert"
5. At the end, take a 3-question comprehension mini-quiz
6. See a comprehension score and option to generate another episode

**Ready-to-Paste Prompt:**

```
Build me a mobile educational app called "LEO Podcast Player".

The app should:
1. Show a topic input screen with a text field and a "Generate Episode" button.
   Include a subtitle: "Learn anything in 3 minutes -- podcast style."
2. When the user enters a topic, use AI to generate a podcast-style script
   lasting approximately 2-3 minutes of reading time. The script should feature
   two speakers:
   - HOST (curious, asks great questions, keeps the pace moving)
   - EXPERT (knowledgeable, explains clearly, uses analogies)
   Format each line as: [SPEAKER] [timestamp] — dialogue text
   Example: [HOST] [0:00] — "Today we're diving into black holes..."
3. Display the script in a chat-bubble-style interface:
   - Host messages appear on the left in a blue bubble
   - Expert messages appear on the right in a green bubble
   - Timestamps shown above each bubble in small gray text
4. After the script ends, show a divider and a "Comprehension Check" section
   with 3 multiple-choice questions about the episode content.
5. Score the quiz and show:
   - 3/3: "Perfect recall!" with a star icon
   - 2/3: "Almost there -- great listening!"
   - 1/3 or 0/3: "Try re-reading the key sections" with highlighted passages
6. Include a "New Episode" button at the bottom.

Design: Podcast-app aesthetic. Dark background (#0f172a) with white and
colored text. Chat bubbles with rounded corners. Host bubbles in soft
blue (#60a5fa), Expert bubbles in soft green (#4ade80). Clean typography
optimized for mobile reading.
```

---

### Idea 3: "LEO Study Planner"

**Concept:** A mobile planning app where students list the topics they need to study, set exam dates, and get an AI-generated study schedule. The schedule adapts as the student checks off completed topics or adjusts their self-rated confidence. Priority scoring considers exam proximity and confidence level -- low confidence + near deadline = highest priority.

**Complexity:** Tier 2 (moderately complex -- combines data input, AI scheduling, and dynamic re-prioritization).

**LEO Feature Showcased:** Adaptive scheduling and personalized learning paths (LEO's adaptive engine sequences content based on mastery state and learning goals -- this prototype shows the scheduling intelligence in a standalone planning tool).

**User Flow:**
1. Add topics to study (e.g., "Calculus - Limits", "Calculus - Derivatives")
2. Set exam dates for each topic or group
3. Rate confidence per topic (1-5 stars)
4. Tap "Generate Study Plan"
5. View a day-by-day schedule with time blocks and topic priorities
6. Check off completed sessions; the schedule re-adapts

**Ready-to-Paste Prompt:**

```
Build me a mobile study planning app called "LEO Study Planner".

The app should:
1. Show an "Add Topics" screen where the student can:
   - Type a topic name (e.g., "Organic Chemistry -- Reactions")
   - Set an exam date using a date picker
   - Rate their current confidence: 1 star (lost) to 5 stars (confident)
   - Tap "Add" to add the topic to their list
   Allow adding up to 10 topics.
2. Show the topic list with each entry displaying: name, exam date,
   confidence rating, and a calculated PRIORITY SCORE.
   Priority formula: topics with low confidence + nearest exam date = highest priority.
   Color-code: High priority (red), Medium (orange), Low (green).
3. Include a "Generate Study Plan" button. When tapped, use AI to create
   a day-by-day study schedule from today until the earliest exam date.
   Each day should list:
   - Which topics to study (1-3 per day)
   - Suggested duration per topic (30, 45, or 60 minutes based on priority)
   - A brief focus tip (e.g., "Focus on practice problems for Derivatives")
4. Display the study plan as a scrollable daily timeline with cards for each day.
5. Each topic card has a checkbox. When the student checks it off as complete,
   recalculate priorities and show a "Plan Updated" toast notification.
6. Include a summary bar at the top: "X of Y topics on track" with a
   progress ring showing overall preparation percentage.
7. Include a "Regenerate Plan" button to get a fresh schedule.

Design: Clean, motivational mobile layout. Primary color: indigo (#4f46e5).
Accent: emerald (#10b981) for completed items. White cards on a light gray
background (#f1f5f9). Progress ring prominently displayed at the top.
```

---

## How to Choose

Use this decision matrix to pick a backup quickly on hackathon day:

| Priority | Google AI Studio Track | FastShot.ai Track |
|----------|----------------------|-------------------|
| **Want something simple and safe?** | LEO Lesson Generator (straightforward input/output, no complex UI) | LEO Flashcard Flow (Tier 1, fewest moving parts) |
| **Want to impress the judges?** | LEO Misconception Detector (novel concept, strong AI showcase, emotional resonance) | LEO Podcast Player (unique format, multi-modal storytelling) |
| **Want to showcase analytics/data?** | LEO Teacher Dashboard Simulator (heatmaps, charts, NL queries -- visual wow factor) | LEO Study Planner (adaptive scheduling, dynamic re-prioritization) |

**Quick heuristic:** If you have less than 90 minutes left, go with the "simple and safe" row. If you have the full 3-hour sprint, go for the "impress the judges" row.
