# StudioFire — Live Broadcast Automation
**Production environment. Real-time streaming system for KDPI.**

---

## 🎯 Quick Start

```bash
# Health check (FIRST - always run this)
./healthcheck.bat

# Run broadcast system
python main.py

# Check logs
tail -f data/logs/*.log

# Deploy to live
bash DEPLOY.md  # Read first!
```

---

## 📁 Key Files

| File | Purpose |
|------|---------|
| `PLAN.md` | Architecture & roadmap (READ THIS) |
| `DEPLOY.md` | Live deployment procedures (HIGH RISK) |
| `REVIEW.md` | Code review notes & audit trail |
| `CHANGELOG.md` | Release history |
| `healthcheck.bat` | Pre-flight diagnostics |
| `main.py` | Entry point |
| `config/` | Configuration files |
| `data/` | Runtime data, logs, recordings |
| `bin/` | Helper scripts |

---

## 🚨 Critical Issues

**Emergency-filler symptom:** See memory `[[studiofire-live-station]]`
- Health-check workflow established
- Logging changes tracked (2026-07-07)
- **Action:** Always run `healthcheck.bat` before going live

---

## 🔧 Tech Stack

- **Language:** Python 3.x
- **Video:** mpv (media player)
- **Streaming:** KDPI protocol
- **Config:** YAML/JSON in `config/`
- **Logging:** `data/logs/`

---

## 📊 Status

- **Git repo:** Yes (`.git/`)
- **Last activity:** 2026-07-07
- **Production:** 🟢 Live
- **Version:** Check `VERSION` file

---

## 🔄 Typical Workflow

1. **Pre-flight:** `./healthcheck.bat`
2. **Check PLAN.md** for current sprint
3. **Make changes** to Python code or config
4. **Test locally:** `python main.py --dry-run` (if supported)
5. **Review DEPLOY.md** before pushing live
6. **Update REVIEW.md** with what you changed
7. **Commit:** `git add . && git commit -m "..."`

---

## 🆘 Troubleshooting

**System won't start?**
→ Run `healthcheck.bat`, check `data/logs/`

**Streaming drops?**
→ Check network, review PLAN.md "Resilience" section

**Unknown error?**
→ See `REVIEW.md` for recent audit trail

---

## 📝 Before Leaving

- [ ] Run `healthcheck.bat` to confirm system is healthy
- [ ] Update `CHANGELOG.md` with any changes
- [ ] Note issues in `REVIEW.md`
- [ ] Commit work: `git status && git commit`

---

## 🔗 Related

- Root guide: `../CLAUDE.md`
- Status dashboard: `../STATUS.md`
- Memory: `c:\Users\markd\.claude\projects\c--Users-markd-Desktop-Projects\memory\MEMORY.md`

