Delete all generated state to start fresh with a new topic.

If the server is running, call `curl -s -X POST http://localhost:3000/api/reset` (this also cancels in-progress generation and kills subprocesses).

Otherwise, delete these files directly:
- CURRICULUM.md
- PROGRESS.md
- LEARNER_PROFILE.md
- TOPIC_REQUEST.json
- CALIBRATION.json
- CALIBRATION_ANSWERS.json
- GENERATION_PROGRESS.json
- lessons/*.md (all lesson files)
- visuals/* (all generated scripts and images)

Do NOT delete: CLAUDE.md, LEARNING_THEORY.md, server.py, index.html, or any app infrastructure.

After cleanup, confirm the slate is clean and the user can pick a new topic.