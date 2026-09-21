import {ansiCodesToString, diffAnsiCodes} from '@alcalzone/ansi-tokenize';

function sameStyles(left, right) {
  if (left === right) return true;
  if (left.length !== right.length) return false;
  for (let i = 0; i < left.length; i++) {
    if (left[i] !== right[i] && (left[i].code !== right[i].code || left[i].endCode !== right[i].endCode)) return false;
  }
  return true;
}

/** Keep upstream transitions/closures; identical adjacent styles need no diff. */
export function serializeStyledChars(characters) {
  let result = '';
  let previous = [];
  for (const character of characters) {
    if (!sameStyles(previous, character.styles)) {
      result += ansiCodesToString(previous.length ? diffAnsiCodes(previous, character.styles) : character.styles);
    }
    result += character.value;
    previous = character.styles;
  }
  if (previous.length) result += ansiCodesToString(diffAnsiCodes(previous, []));
  return result;
}
