# Jugnu

A personal voice assistant. Jugnu is Hindi for firefly: a small light that glows when it wakes. It also stands for Just a Useful Gadget, Not Ultron.

Say "Hey Jugnu, ..." and talk. The brain you chose answers, and Jugnu says it out loud.

## What you need

Jugnu runs on Windows. `install.bat` brings Python and Jugnu's libraries. What else you need depends on what you turn on:

| For | You need |
|---|---|
| The brain | One of: a Claude Pro/Max plan; a ChatGPT plan (plus Node.js, for Codex); an API key from Anthropic, OpenAI, Google (Gemini), DeepSeek, xAI or OpenRouter; or a model in Ollama |
| Voice and ears, Google | A Google Cloud project with billing, with the Text-to-Speech and Speech-to-Text APIs enabled, and the gcloud CLI |
| Voice and ears, this computer | An NVIDIA GPU with about 4 GB of memory free |
| A browser | Node.js and Chrome (and, only to drive your own Chrome, the Playwright MCP Bridge extension) |
| Google Drive and Gmail | An OAuth client from a free Google Cloud project of your own (steps below) |
| The council, hands-on | Docker Desktop with GPU support |

## Install and set up (Windows)

Double-click `install.bat`. It installs Python 3.12 and Jugnu's libraries inside this folder
(`.venv`, nothing system-wide), then opens setup, a small window that walks you through:

- You: what it calls you, and how it talks to you (Colleague, Friendly, Brief, or your own words).
- The brain: Claude, GPT, Gemini, DeepSeek, Grok, OpenRouter, a local model through Ollama,
  or any OpenAI-style server. Claude and GPT can run on your plan or on an API key. On a plan,
  Claude signs in through Claude Code's own login and GPT goes through Codex, where replies are
  a tad slower. The others need an API key (Gemini's is free from Google AI Studio). Setup
  checks the key and lists the models to pick from.
- The council: which models it can ask (the same choices as the brain, never the brain itself),
  and what they must never be sent.
- Voice and ears: Google, which is natural and fast and needs a Cloud account with billing, or
  Chatterbox and Whisper on your own NVIDIA GPU, where nothing leaves your computer. Setup
  installs those two if you pick them.
- Extras: a browser, Google Drive and Gmail, and where reports go.

Setup writes your choices to `.env` and starts Jugnu. To change them later, open setup again from
the tray icon's Settings (or run `.venv\Scripts\python setup_jugnu.py`), or edit `.env` by hand.
Every setting is explained in `.env.example`.

Every brain gets the same hands. With Claude they are the Agent SDK's. With any other brain they
are Jugnu's own (`brain.py`). Either way it can read and edit your files, run commands, and use
PubMed, the council, the browser and Drive, and it asks you out loud before the risky actions it
knows of. "What it asks before doing" below lists those actions and their limits.

A few details differ by brain:

- Web search. Claude and Gemini use their makers' own search, and GPT on a ChatGPT plan uses
  Codex's. Every other brain searches through DuckDuckGo's plain results page. That needs no key,
  but the page sometimes refuses programs, and then the brain is told to search in the browser.
- Pictures. Any brain can open an image file and is shown it. A model that takes no pictures says
  so and carries on.
- GPT on your ChatGPT plan. Codex brings its own hands for files, commands and web search, and
  Jugnu answers its permission requests the same way. The council, PubMed, the browser and Drive
  are handed to Codex as tools and run by Jugnu, with the same checks. This relies on a part of
  Codex that Codex itself marks experimental. A Codex that won't take those tools still works with
  its own hands, and Jugnu says so in the log.

## Things you can say

| Say | What happens |
|---|---|
| "Hey Jugnu, ..." | Wakes it (so does Wake in the tray menu). Once it's awake, just talk |
| "Go to sleep", "stop listening", "bye" | It sleeps; the program keeps running |
| "Stop", "cancel that", "never mind" | Drops the job it's on |
| "How's it going?" | Where a long job stands |
| "Think hard about ..." | That answer comes from the deeper model |
| "Switch to Haiku / Sonnet / Opus", "go back to automatic" | Keeps one Claude model, or goes back to picking per request (Claude as the brain) |
| "Switch to Grok / Gemini / DeepSeek / GPT", "switch back to Claude" | A council member takes over as the brain, with the same hands and the same say as your usual one, and hands back |
| "Switch to Argon", "switch to Grok 4.6" | The same, on any model that member's key has. The name is looked up live, so new models need no update |
| "Switch on the learning mode" | It asks what to learn. For the basics, you read out its name, the commands and your brains, and it keeps how your voice gets written down (`heard.txt`). For a word of your own, you say it and what it is, it works out the spelling with you, and speech recognition is told to expect it |
| "Apply the change", "undo the last change" | With self-improve mode on (the tray): swaps in a change it prepared to its own program once its checks pass, or puts the old files back |
| "Go private", "private mode off" | Voice and ears on your GPU instead of Google, and back |
| "Log everything" | Saves this conversation, readable, to your reports folder |
| "Start fresh" | Begins a new conversation |
| "Learn my voice" | You read a few lines and it learns your voice. From then on other people's words are ignored, "stop" and "go to sleep" included (Sleep and Quit in the tray menu still work). "That wasn't me" makes it stricter when someone else gets through, "listen to everyone" turns it off, "only listen to me" turns it back on, and "forget my voice" drops it. All of this stays on this computer (`whose.py`) |
| "Show the browser", "hide the browser" | Brings its own browser's window onto your screen (to sign in somewhere, say), or puts it out of sight. It works the same either way |
| "Turn your vision on" / "Look at my screen" | Sends a picture of your screen with each thing you say, until "turn your vision off" or it sleeps |
| "Yes", "no", "don't ask" | Your answer when it asks before something risky. "Don't ask" covers the rest of that request |
| "Be more formal", "make Qwen teal", "you're saying nginx wrong" | Changes its manner, a colour of its face, or how it says a word |

## Talking to it

Jugnu greets you when it starts, already listening. Talk normally and it answers when you go
quiet. "Go to sleep", "stop listening" or "bye" sends it to sleep (the program keeps running), and
"Hey Jugnu" or Wake in the tray menu wakes it again. After 3 minutes with nobody talking to it,
Jugnu dozes off by itself, so a conversation in the room later doesn't go to its brain.

Talk over it and it stops and listens. An echo canceller (WebRTC's) removes its own voice from the
mic, so speakers are fine. The canceller needs about six seconds of Jugnu's voice to learn the
room, so the start of the greeting can't be interrupted.

It speaks each sentence as soon as it's written, without waiting for the whole answer. When you
pause for a second (`JUGNU_PAUSE` in `.env`) after a sentence that sounds finished, it answers.
Trail off ("and...", "so...", "um") and it gives you two seconds. If its answer isn't ready in a
moment it says a short filler ("Hmm.", "Let me look into that."), recorded at startup so it plays
instantly.

"Log everything" saves this conversation, readable, to your reports folder. "Start fresh" (or "new
conversation") begins a new one, which a restart then picks up instead of the old. Conversations
are saved to `logs/` (never committed). Show conversation in the tray menu shows what it hears and
says as it happens, with how long each reply took.

Put a few lines about yourself in `about_me.md` (never committed) and Jugnu reads them before
every conversation, so "Mercury" means the planet, not the metal.

Speech that isn't for it shows there as `(heard, not for me: ...)`. If it missed its name, that
line shows how it was heard. Add the spelling to `NAME` in `ears.py`.

## Voice and ears

### Hearing

Google Speech-to-Text (Chirp 3, `en-IN`) hears you, streamed. Your voice goes to Google as you
speak, so the words are ready a moment after you stop. A small speech detector (Silero) runs on
your laptop, so only speech is sent, never silence. While Jugnu is asleep, Google hears just the
first 2.5 s of anything said, which is enough for "Hey Jugnu". It hears the rest only if it was
for Jugnu.

This needs three things: the Speech-to-Text API enabled in your Cloud project,
`JUGNU_GOOGLE_PROJECT` (the project's ID) in `.env`, and your Google login saved once with
`gcloud auth application-default login` (Chirp 3 doesn't take API keys).

`python ears.py google` checks it all and times a reply. `python ears.py mics` shows how loud
your mic hears you. Set `JUGNU_STT_REGION=us` if `eu` (the default) is slow for you, and
`JUGNU_STT_LANGUAGE` (default `en-IN`) for your accent, e.g. `en-US` or `en-GB`.

### Speaking

The voice is Google's Chirp 3 HD voice `en-IN-Chirp3-HD-Algieba`. It uses `GOOGLE_TTS_API_KEY`
from `.env`, a Google Cloud API key restricted to the Text-to-Speech API. Nothing loads on your
GPU for it. If Google can't be reached, Jugnu goes quiet (replies still show as text) and tries
again a minute later. `JUGNU_GOOGLE_VOICE` picks another Google voice, and `JUGNU_VOICE=off` in
`.env` keeps it silent. How it says tricky terms (nginx, Nguyen) is set in `pronunciations.txt`.

### Private mode

Say "go private" to turn it on, and "private mode off" or "switch back to online mode" to turn it
off. The tray menu does the same.

Private mode loads Chatterbox and Whisper large-v3-turbo on your GPU, which takes about half a
minute and ~4 GB of GPU memory, and switches voice and ears to them. Nothing you say or it says
goes to Google, and its face turns ruby red while it's on. Turning it off goes straight back to
Google and unloads them. If Google stops answering for half an hour, the tray pops up a
suggestion to go private.

Your questions still go to the brain you chose either way. Private mode keeps your voice off
Google; Jugnu still needs the internet to think.

In private mode its voice copies `voices/chatterbox_male.wav`. Without that file it speaks in
Chatterbox's built-in voice.

### Make it yours

How it talks to you lives in `personality.md`. Setup's first screen starts it from Colleague,
Friendly or Brief, or your own words. The colours of its face, per model or council member, are
in `colors.txt`. You can change either by telling it ("be more formal", "make Qwen teal").
Personality sets tone and depth only. Its short spoken style and the spoken OK before anything
risky can't be talked away.

## What it can do

### Files, the web and papers

It reads and searches your files, opens PDFs, Word files and images directly, edits files and
runs commands. It searches the web and reads pages on its own, and says "One moment, let me look"
first so you're not left in silence. For the literature it searches PubMed directly (NCBI's API,
newest first) and reads abstracts before describing a paper.

### The browser

Jugnu drives a browser of its own through Microsoft's Playwright MCP, which needs Node.js and
Chrome. That browser is apart from yours and every brain shares it, so sign in there once to the
sites you want it to use. "Hide the browser" puts its window out of sight and "show the browser"
brings it back. `JUGNU_BROWSER=hidden` in `.env` starts it out of sight. `JUGNU_BROWSER=chrome`
drives your own Chrome instead, through the "Playwright MCP Bridge" extension.

### What it asks before doing

Almost all of this runs without asking. It asks out loud only before risky actions: deleting or
moving files, overwriting a file, rewriting git history, running downloaded code, system changes,
stopping programs, touching password or key files, and clicking things like Send, Submit, Pay or
Delete in the browser.

The list is `risk()` in `jugnu.py`. It is a list of known patterns and there is no sandbox behind
it. A command worded in a way the list doesn't know runs without asking, so it can't guarantee
anything. Treat it as a best effort.

Answer however you'd say it ("yes", "go for it", "no, leave it", or "don't ask" for the rest of
that request), by voice or with the tray's Allow and Deny. No answer in a minute
counts as no.

### Which model answers

With Claude as the brain, each request picks its own model. Conversation goes to Haiku, which
answers quickest. Anything with work in it (reading, searching, files, papers, code, the browser)
goes to Sonnet. Opus answers when you say "think hard about..." or "ask Opus...". A short "yes,
go ahead" right after a task stays with the model doing that task. Say "switch to Haiku / Sonnet
/ Opus" to keep one until you say "go back to automatic".

Another brain does the same with the conversation, work and "think hard" models you picked in
setup.

The council's Gemini and DeepSeek can be changed the same way ("use Argon for Gemini"). Jugnu
checks which Gemini models your key really has, tries the new one, and remembers the choice
(`logs/council_models.json`).

### Switching brains

No model is the senior one. The brain you chose in setup is only the usual one. Say "switch to"
and the name of any council member, or of any model its key has, and that one becomes the brain
and picks up the conversation where the last left it. It gets the same hands (files, commands,
PubMed, the browser and Drive, and the council, which now includes your usual brain) and the same
checks before anything risky.

A brain you switch to by its family name ("switch to Gemini", "switch to Grok") answers on that
family's quick model, the way Claude starts on Haiku. "Think hard" gets its main model for that
one answer. A model you name ("switch to Argon") is used for everything. A stand-in runs on its
own API key, billed per use, so each answer's token count shows in the log.

### The council reads documents

Ask what the council thinks of a report, a run's results or any document, and each member opens
and reads those files itself. It has the same reading tools a brain has, limited to the files it
was shown. Nothing is run, and it needs no Docker.

### The council, hands-on

Ask the council to audit or review a folder and each member works on it in its own throwaway
Docker container. Claude works through Claude Code, GPT and Grok through Codex, and Gemini
through Gemini CLI. DeepSeek, OpenRouter and a local model work through Claude Code pointed at
their own servers. An "other server" seat answers questions and reads documents, but can't work
hands-on. The container holds a copy of the folder and nothing else from your machine. The member gets your GPU and full freedom inside it. Their findings and run transcripts
land in `Documents\Jugnu workspaces`, and Jugnu's report merges them.

The container keeps a member away from the rest of your machine. The internet is open inside it,
though, and so is that member's own key or sign-in, so point the council only at code you trust.

It needs Docker Desktop with GPU support. Run `python auditors.py build` once (about 10 GB), then
`python auditors.py check` to see each member run something.

- If GPT's safety filter declines a job, GPT sits it out. A GPT model that isn't on your plan
  passes to the next one (`JUGNU_GPT_MODELS`).
- Each member has a time limit, `JUGNU_AGENT_MINUTES` (30).
- Members take turns, one at a time. `JUGNU_AUDITORS_AT_ONCE=2` lets two work at once.

While they work, Jugnu tells you when there's news: who finished, and who's running the tests or
installing packages (read from the processes in each container). Ask "how's it going?" any time
for where each one is. "Stop" kills them.

### Long jobs and reports

Big jobs (audits, code or pipeline reviews, comparisons across many files) end in a written
report in `Documents\Jugnu reports` (`JUGNU_REPORTS` in `.env` moves it). Findings are ranked by
severity, each with file, line and fix. Aloud it gives only the headline.

Talk over it and it stops. The rest of that answer stays on screen, marked `(unsaid)`, and its
next answer knows where you cut it off. On a long job it says what it's doing after 20 quiet
seconds, then only when there's news (or every 2.5 minutes).

Say "stop", "cancel that" or "never mind" to drop the job. "Cancel that, read the README instead"
drops it and starts the new one. Anything else you say mid-job goes to a side voice, a quick
model with no tools. A question it can answer from the conversation gets answered on the spot
while the job carries on, and the brain hears of it after. Work it can't do there (an
instruction, a search, a file) waits until the job's done.

### Self-improve mode

Self-improve mode is off unless you turn it on from the tray. While it's off, no brain can change
Jugnu's own program.

While it's on, Jugnu goes through each day's log while it's asleep. Mishearings it is sure of go
straight into `heard.txt`. Anything that would take a change to the program is written to
`logs/suggestions.md` and raised when you're back. A change is made on a copy (`_next`), never on
the running files. "Apply the change" runs the copy's checks and swaps it in for the next start,
keeping the old files in `_previous`. "Undo the last change" (or the tray) puts them back.

Its safeguards (`guard.py`) are off-limits to every brain in every mode.

## Google Drive and Gmail

Jugnu can search, read, create and edit your Google Drive, Docs and Sheets through
[workspace-mcp](https://github.com/taylorwilsdon/google_workspace_mcp) (MIT), which `uvx` fetches
and runs. It can never delete or trash anything there, clear cells, or change sharing. Those are
refused in `drive_block()`, because Google has no "edit but never delete" permission.
workspace-mcp is pinned in `jugnu.py` to the version those checks were written for. Replacing a
whole file's contents, or merging cells, waits for your spoken OK.

In Gmail it reads, searches, labels, archives and writes drafts. It can't send, trash, mark as
spam, or touch filters. Sending and filters aren't loaded at all, and the rest is refused in
`drive_block()`.

It needs an OAuth client from your own (free) Google Cloud project, once:
1. At console.cloud.google.com, create a project called Jugnu.
2. APIs & Services > Library: enable Google Drive API, Google Docs API, Google Sheets API and Gmail API.
3. Google Auth Platform: Get started. App name Jugnu, your email for support and
   contact, Audience External.
4. Audience > Test users: add your Gmail address.
5. Clients > Create client: Application type **Desktop app**, name Jugnu. Copy the
   client ID and secret.
6. In `.env`: `GOOGLE_OAUTH_CLIENT_ID=...`, `GOOGLE_OAUTH_CLIENT_SECRET=...`,
   `USER_GOOGLE_EMAIL=you@gmail.com`. Restart Jugnu.
7. Ask it something about your Drive. It opens Google's sign-in in your browser and
   warns that the app isn't verified (it's yours): Continue, Allow.

While the app is in Testing, Google makes you sign in again every 7 days. To stop
that, Audience > Publish app. If sign-in fails with `redirect_uri_mismatch`, make a
**Web application** client instead, with `http://localhost:8000/oauth2callback` as
its redirect URI.

## Running it all day

Open Jugnu from the Start menu, or use the Jugnu shortcut in this folder. `install.bat` makes
both, with Jugnu's icon. Jugnu loads once and lives in the system tray as a "J" by the clock:
amber while loading, grey asleep, green listening, blue talking, red if it stopped.

Its face (`face.py`, showing `face/face.html`) is an orb of light floating over your desktop with
no window around it. Waking, the orb rises out of a soft glow at the bottom edge. Asleep, it sinks
back in and disappears. Thinking, it spins up and a band of light travels over it. Talking, the
voice runs round it in ridges.

The face first opens big on your last screen (the second monitor, if you have one). Over the face, drag to move it, Ctrl + scroll to
resize it, and scroll to make it brighter or dimmer. It remembers where you left it and how
bright (`logs/face.json`; delete it to reset). Other windows can cover it, and clicks on the empty
space around it go through to whatever is behind.

Right-click the tray icon for Wake, Sleep, Allow and Deny (when it asks), Private mode, Face (hide
or show it, bigger, smaller, brighter, dimmer), Only my voice, Vision, Self-improve mode and its
undo, Show conversation (a live view of what it hears and says), Settings (stops Jugnu and opens
setup, whose last screen starts it again) and Quit. Only Quit and Settings stop it. Closing a
window doesn't.

To start it at login, tick "Start me when you log in" on setup's last screen (Settings in the
tray opens it), and untick it there to stop that. It is off unless you tick it.

`python jugnu.py` is the console version, for debugging. It is the only place you can type: a
message instead of saying it, `!private` to switch private mode, or `!fix <better answer>` to log
a correction. Space there wakes it or cuts it off.

`run_checks.bat` runs every module's own checks and prints ok for each. It needs no keys,
microphone or internet.

## Uninstall

Double-click `uninstall.bat`. It stops this copy of Jugnu and removes its shortcuts, its
start-at-login entry and the Python environment `install.bat` made. Then it asks, one at a time,
before deleting anything of yours or anything another copy of Jugnu might share:

- `.env` (your settings and keys)
- `logs` (your conversations)
- the reports and council workspace folders
- the downloaded voice and ears models
- the saved Drive and Gmail sign-in
- the council's Docker image

Shared tools stay (uv, Python, Node.js, Claude Code, Codex, gcloud), and it tells you where to
take back Jugnu's access to your Google account. After that the folder holds only code. Delete it
when you like.

## Credits

Built with help from Claude (Anthropic).

Speech recognition by Google Cloud [Speech-to-Text](https://cloud.google.com/speech-to-text) (Chirp 3), with
[Silero VAD](https://github.com/snakers4/silero-vad) (MIT; `models/silero_vad.onnx`, run by [ONNX Runtime](https://onnxruntime.ai), MIT) to hear when you start and stop,
and WebRTC's echo canceller via [LiveKit](https://github.com/livekit/python-sdks) (Apache 2.0).
Literature search by NCBI's [E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/) (PubMed).
Browser control by [Playwright MCP](https://github.com/microsoft/playwright-mcp) (Microsoft, Apache 2.0).
Telling your voice from others' by a [WeSpeaker](https://github.com/wenet-e2e/wespeaker) ResNet34 model trained on
VoxCeleb (CC BY 4.0), downloaded the first time it learns your voice.
Argon mode uses Google's [Gemini API](https://ai.google.dev) and reads PDFs with [pypdf](https://github.com/py-pdf/pypdf) (BSD).
Google Drive, Docs and Sheets through [workspace-mcp](https://github.com/taylorwilsdon/google_workspace_mcp) (MIT).
Speech by Google Cloud [Text-to-Speech](https://cloud.google.com/text-to-speech) (Chirp 3 HD).
The face is an orb of points of light drawn with [Three.js](https://threejs.org) (MIT; bundled in `face/three`)
in a [pywebview](https://pywebview.flowrl.com) window (BSD). It is worked out from a few numbers and is modelled on no one's face.
