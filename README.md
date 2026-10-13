# seedbank

Find stuff that's already been built, faster.

Seedbank is a thin layer over git. A **seed** is a git repo, or a folder in one, with a short `seed.json` that says what
it is. The repo can live anywhere git does: GitHub, GitLab, Codeberg, or your own server. Seedbank adds three things: a
common format, a catalog to search, and one command to fetch any seed. Hosting, versions, access and signatures stay
plain git and whatever host you already use.

> **Status: the command runs.** `check`, `publish`, `share`, `search`, `fetch`, and `steps`.

## Make a seed

Add `seed.json` to the repo, or to the folder that is the seed:

```json
{
  "kind": "tool",
  "description": "Fix FLAC tags from a MusicBrainz release id."
}
```

- `kind`: `tool`, `app`, `game`, `component`, `prompt`, `skill` or `bundle`.
- `description`: one sentence, at most 160 characters. Search matches on it.

Then commit and push. That's a valid seed.

| Kind | Also needs |
|---|---|
| `tool` | `SKILL.md`: when to use it and how to run it |
| `app`, `game` | the built UI committed in `ui/` (`ui/index.html` opens) |
| `component` | `ui/` or `skin/` |
| `prompt` | `PROMPT.md`, with blanks written as `{{name}}` |
| `skill` | `SKILL.md` in the Agent Skills format (`name` and `description` at the top) |
| `bundle` | `"members": [<seed links>]` in seed.json |

Optional in `seed.json`: `name` (defaults to the folder or repo name), `tags` (extra search words), `dependencies`
(other seeds, as links), `requires` (what it needs to run, in plain words, for example `["macos", "tailscale"]`), and
`permissions` (for example `["net", "fs.write"]`). Fetch shows `requires` and `permissions` before you use a seed.
`admin` in `permissions` means it changes system settings (creates users, installs services, runs with sudo), so you
run it yourself; WizardOS never installs it for you.

Everything else comes from git, so don't repeat it in seed.json:

| You want | Use |
|---|---|
| a license | a `LICENSE` file; seedbank recognises the common ones |
| versions | git tags |
| big files | Git LFS |
| proof of who made it | signed commits (`git commit -S`) |
| a picture, a longer description | `README.md` and a `thumbnail.png` (16:10) in the seed's folder |

To help people and AI agents set it up, end `SKILL.md` or `README.md` with an `## Integrate` section, one
`### <environment>` per place it runs. A step can point at another seed that does a setup chore, by its link.

**No network, faster listing** *(planned)*. An app or component that never needs the network can say so by making
this the first line inside `<head>` of every HTML page, and loading its scripts only from its own folder:

```html
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; connect-src 'none'; img-src 'self' data:; form-action 'none'; base-uri 'none'">
```

The browser then blocks every network call, so the catalog can list it without a manual review.

## Links

A seed's link is its git URL, plus the folder after `//` and a tag or commit after `@` when needed:

```
codeberg.org/sam/tools//flac-tagger@v1.0
github.com/ana/notes-app
git.example.org/you/wizard-apps//apps/grimoire@3f9a1c2
```

Seeds in the public catalog also get a short name, `owner/name` (for example `sam/flac-tagger`).

## Commands

| Command | What it does |
|---|---|
| `seedbank check` | Says what's missing or wrong in this seed, how to fix it, and *(planned)* which review it will get. |
| `seedbank publish` | Submits the seed's link to the public catalog. The repo must be publicly readable. |
| `seedbank share <user>…` | Gives those people read access on your git host, then prints the link to send them. |
| `seedbank search <words>` | Searches the public catalog. |
| `seedbank fetch <seed> [folder]` | Downloads a seed, by link or short name. Never installs or runs it. |
| `seedbank steps <seed> --env <environment>` | Prints the seed's Integrate steps with every helper seed's steps filled in. |

## Publish

Push your repo somewhere public, then:

```
$ seedbank check
$ seedbank publish
  submitted  codeberg.org/sam/tools//flac-tagger
  name       sam/flac-tagger (once accepted)
```

The public catalog is itself a git repo; `publish` sends it your link, as a pull request where the catalog is hosted.
Once accepted, the seed shows up in `seedbank search` and on [etienne.world/seedbank](https://etienne.world/seedbank/).
A group of friends can also run their own catalog in any git repo: `seedbank publish --catalog <git url>`.

The catalog lists the exact commit it reviewed, not whatever your repo holds later. To update, push and run
`seedbank publish` again. *(Planned.)* Each request gets one of four reviews, picked by automatic checks that read
your files and never run them:

| Your seed | Review |
|---|---|
| Only text and images (prompts, skills, docs) | Listed automatically |
| HTML/CSS, or a web app with the no-network line above | Listed automatically |
| A small script | A short human review |
| Uses `admin` or `net`, ships programs, or is a lot of new code | A full human review |

Seeds built from already-listed seeds, as `dependencies` pinned with `@`, or as an unchanged copy of a listed seed's
folder, only have their new code reviewed. Their permissions still count.

## Share privately

Keep the repo private and run:

```
$ seedbank share ana theo
  gave ana, theo read access on codeberg.org/sam/tools
  link codeberg.org/sam/tools//flac-tagger@v1.0
```

Seedbank does this through your host's own API for GitHub, GitLab and Forgejo/Gitea (Codeberg). For any other host, it
tells you what access to grant there and prints the link. Either way, the host decides who can read: fetch works for the
people you added, with their own git login, and stops when you remove them. Access covers the whole repo, not one
folder, so keep private seeds in their own repo. (On a personal GitHub account, collaborators can also push; use a free
organization for read-only.)

## Fetch

```
$ seedbank fetch sam/flac-tagger
  tool   Fix FLAC tags from a MusicBrainz release id.
  from   codeberg.org/sam/tools//flac-tagger @ v1.0 (commit 3f9a1c2, signed by sam)
  needs  python3
  asks   network, write files
  wrote  ./flac-tagger
```

Fetch is a git clone of just that folder, using your own git setup, so it reaches every public seed and every private
one you can read. It pins the exact commit, so the same link always gives the same files. It doesn't install or run
anything: read the seed's `SKILL.md` first, since a seed you run yourself can do anything your account can. Seeds you've
fetched and catalog searches keep working offline.

On WizardOS, **Pull** in the launcher does the same and then installs the seed with only the permissions you approve.
A seed that asks for `admin` is only fetched; you run it yourself, following its `SKILL.md`.
