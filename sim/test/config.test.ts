import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { workerCount } from '../src/config';

test('vertical scaling is explicit and no longer silently capped at four', () => {
	assert.equal(workerCount('16'), 16);
	assert.equal(workerCount('0'), 0);
	assert.equal(workerCount(''), 2);
	for (const value of ['4junk', '1.5', '-1', '257', 'NaN']) assert.throws(() => workerCount(value));
});
