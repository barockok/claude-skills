# claude-skills

Agent skills by [barockok](https://github.com/barockok), packaged as a Claude Code plugin marketplace. Each skill is a plain `SKILL.md` folder, so it also works with other agents that read skills.

## Skills

| Plugin | What it does | Platforms |
|---|---|---|
| [video-analyze](plugins/video-analyze/skills/video-analyze/SKILL.md) | Watch a video from a URL (YouTube, TikTok, Instagram, X, Vimeo) or a local file. Produces a timestamped transcript, scene keyframes and contact sheets, then a structured analysis you can ask follow-up questions about. | macOS, Linux |

## Install

### Claude Code plugin

```
/plugin marketplace add barockok/claude-skills
/plugin install video-analyze@barockok-skills
```

### Plain skill (any agent that reads `SKILL.md`)

```bash
git clone https://github.com/barockok/claude-skills
cp -r claude-skills/plugins/video-analyze/skills/video-analyze ~/.claude/skills/
```

## video-analyze

Requirements: Python 3.8+, `ffmpeg`, `yt-dlp` (for URLs), and one transcription backend. The script picks one automatically:

| Backend | Best for | Install |
|---|---|---|
| `mlx` | macOS on Apple Silicon | `uv tool install mlx-whisper` |
| `faster-whisper` | Linux, servers, Intel Macs (CPU or CUDA) | `python3 -m pip install faster-whisper` |
| `whisper` | Fallback anywhere (slow on CPU) | `uv tool install openai-whisper` |

Check your setup:

```bash
python3 plugins/video-analyze/skills/video-analyze/scripts/video_analyze.py doctor
```

Then ask your agent "analyze this video: <url>". Artifacts land in `~/.video-analysis/<id>/`:

```
manifest.json      metadata, caption, frame list, transcription backend
segments.json      timestamped transcript
transcript.srt     subtitles
frames/            scene keyframes (768 px)
sheets/            6x6 contact sheets for a fast visual overview
analysis.md        the structured analysis the agent writes
```

Standalone CLI:

```bash
VA=plugins/video-analyze/skills/video-analyze/scripts/video_analyze.py
python3 $VA run "https://www.youtube.com/watch?v=..." [--lang en] [--backend faster-whisper --model small]
python3 $VA frame <id> 01:23 --n 3 --width 1280   # zoom on a moment
python3 $VA slice <id> 01:00 01:30                # re-transcribe a stretch
python3 $VA list
```

Music-only clips often make Whisper hallucinate (looping text). The script flags this as `transcript_suspect`, and the skill tells the agent not to trust that transcript.

## License

MIT
