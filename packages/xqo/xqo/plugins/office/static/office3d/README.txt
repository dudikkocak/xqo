3D office models, all by Kenney (www.kenney.nl), CC0 (see the LICENSE files next to them):
- furniture/: Furniture Kit 2.0 (desk, chairDesk, computerScreen, computerKeyboard, computerMouse, pottedPlant)
- characters/: Mini Characters 1.0 (4 characters with their animations, Textures/colormap.png)
Taken unchanged from the npm package orbitdesk 0.0.1 (MIT), which bundles these Kenney files.
catalog.json lists what the office can place: Kenney models ("glb", relative to this folder) and furniture built
in office3d.js from simple shapes ("builtin": bookshelves, whiteboards, coffee bar, fridge, beanbags, rug, windows,
sofa, meeting table, partition wall, lamp, server rack), with their size in tiles, whether agents walk around them
and the spots agents use. To ship more of Kenney's kits, put the .glb files here and add entries to catalog.json
(and characters under "characters"). An HQ can also upload its own models in the office's Edit mode; those live
in <data dir>/artifacts/office-assets/, not here.
