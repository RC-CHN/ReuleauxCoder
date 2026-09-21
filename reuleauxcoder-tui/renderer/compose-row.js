/*!
 * Cell overlay semantics adapted from Ink 7.1.1 (MIT).
 * Copyright (c) Vadym Demedes <vadimdemedes@hey.com> (https://github.com/vadimdemedes)
 * Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy of
 * this software and associated documentation files (the "Software"), to deal in
 * the Software without restriction, including without limitation the rights to
 * use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
 * the Software, and to permit persons to whom the Software is furnished to do so,
 * subject to the following conditions:
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
import {serializeStyledChars} from './serialize.js';

/** Writes have already been clipped and transformed by Ink, in paint order. */
export function composeRow(writes, width, caches) {
  const key = JSON.stringify([width, writes]);
  const cached = caches.rows.get(key);
  if (cached !== undefined) return cached;
  const space = {type: 'char', value: ' ', fullWidth: false, styles: []};
  const cells = [];
  for (let x = 0; x < width; x++) cells.push(space);
  for (const {x, line} of writes) {
    const characters = caches.getStyledChars(line);
    if (!characters.length) continue;
    let offset = x;
    // Overwriting the trailing half of a wide character also clears its head.
    if (cells[offset]?.value === '' && offset > 0 && caches.getStringWidth(cells[offset - 1]?.value ?? '') > 1) cells[offset - 1] = space;
    for (const character of characters) {
      cells[offset] = character;
      const characterWidth = Math.max(1, caches.getStringWidth(character.value));
      for (let index = 1; index < characterWidth; index++) {
        cells[offset + index] = {type: 'char', value: '', fullWidth: false, styles: character.styles};
      }
      offset += characterWidth;
    }
    // Replacing the head must not leave the old trailing placeholder behind.
    if (cells[offset]?.value === '') cells[offset] = space;
  }
  const result = serializeStyledChars(cells.filter(cell => cell !== undefined)).trimEnd();
  caches.rows.set(key, result);
  return result;
}
