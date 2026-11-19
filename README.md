# GoF2R Mods

The community mod catalog of the [Galaxy on Fire 2 remake](https://github.com/JoppieToppie/Galaxy-on-Fire-2-Unity-Remake).
The game's **Mods > Mod Browser > Online** tab lists every mod here, with its preview, description, author, views and
downloads, and installs it with one click (updates too).

## Players

Nothing to do here: open the Online tab in the game. Mods marked **Scripts** run Lua scripts, which the game asks you to
allow when you turn the mod on; only allow scripts from authors you trust.

## Submitting a mod

1. Make your mod (by hand, see the [modders' guide](https://github.com/JoppieToppie/Galaxy-on-Fire-2-Unity-Remake/blob/main/Modding/README.md),
   or with the game's Mod Builder) and zip it: `mod.json` at the zip's root or in one top folder.
   - `mod.json`'s `name`, `version`, `author` and `description` are what the catalog shows.
   - A `preview.png` (or the file `mod.json`'s `preview` names), 16:9 looks best, becomes the mod's picture. Without one,
     the catalog makes a card with its name.
2. Put the zip somewhere with a **direct download link** (the link must give the .zip file itself, not a web page):
   - a release of your own GitHub repository (the asset's link), best;
   - Dropbox: the share link with `?dl=1` at the end;
   - any other https link that downloads the file. MediaFire, Mega and Google Drive pages don't work.
3. Open a pull request here that adds one file, `submissions/<your mod's id>.json` (the `id` in your `mod.json`):

   ```json
   {
     "download": "https://github.com/you/your-mod/releases/download/v1.0/your_mod.zip",
     "owner": "your-github-name"
   }
   ```

   GitHub's "Add file > Create new file" button on the [submissions](submissions) folder does this from the browser.
4. The **Check submission** check downloads your zip and checks it as the game reads it. Once a maintainer merges the
   pull request, the mod is copied into a release of this repository and shows in the game within a few minutes.

**Updating:** raise `version` in your `mod.json`, upload the new zip and change `download` in your submission file by
pull request. Only the `owner` (and the maintainers) can change a mod's submission. Removing your submission file
takes the mod out of the catalog.

**Rules:** your own work, or work you have permission to share (name it in `mod.json`'s `credits`); nothing that runs on
its own (`.exe`, `.dll`, shell scripts and the like are refused: mods are data, models, textures, sounds and Lua scripts);
at most 1 GB. The maintainers may remove any mod.

## How it works

| File | What it is |
| --- | --- |
| `submissions/<id>.json` | One per mod: its download link and owner. The only files a pull request may change. |
| `catalog.json` | What the game reads, written by the workflows only. |
| `tools/catalog.py` | The checker and publisher (`python tools/catalog.py inspect my_mod.zip` checks a zip like the workflow does). |

- **Check submission** (pull requests): who may change what, each changed submission's zip downloaded and checked.
- **Publish** (a submission merged): the zip checked again and released here as `<id>-v<version>` with `<id>.zip`,
  `<id>-preview.jpg` and `<id>-thumb.jpg`; `catalog.json` updated. The game only downloads from these releases and
  checks each download's SHA-256 against the catalog.
- **Stats** (every three hours): views and downloads from the release assets' download counts: `<id>.zip` fetched =
  a download, `<id>-preview.jpg` fetched = a view (the game loads it when a player opens the mod's page).
