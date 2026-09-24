import assert from 'node:assert/strict';
import test from 'node:test';
import {compatibilityProblem, minimumCoreVersion, minimumEditorApiVersion} from '../src/core/compatibility.js';
import {setLocale} from '../src/i18n.js';

test('core compatibility checks release versions and same-release editor revisions', () => {
  const info = {core_version: minimumCoreVersion, editor_api_version: minimumEditorApiVersion};
  assert.equal(compatibilityProblem(info), undefined);
  assert.equal(compatibilityProblem({...info, core_version: '999.0.0', editor_api_version: 100}), undefined);
  assert.equal(compatibilityProblem({...info, core_version: `${minimumCoreVersion}.post1+local`}), undefined);
  assert.match(compatibilityProblem({...info, core_version: '0.9.99'})!, /too old/);
  for (const version of [undefined, '', '0.11', 'v0.11.0', `${minimumCoreVersion}rc1`]) {
    assert.match(compatibilityProblem({...info, core_version: version})!, /release version/);
  }
  for (const revision of [undefined, 0, '1', -1, 1.5]) {
    assert.match(compatibilityProblem({...info, editor_api_version: revision})!, /integration revision/);
  }
  setLocale('zh-CN');
  try {assert.match(compatibilityProblem({...info, editor_api_version: undefined})!, /更新核心/);}
  finally {setLocale('en');}
});
