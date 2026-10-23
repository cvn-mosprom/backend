// The vendored lib references `three` only in type positions (minigames/types.ts). Stub the few names so the
// typecheck does not need the real package (same trick as sim/types/three.d.ts).
declare namespace THREE {
	type Object3D = any;
	type Group = any;
	type Camera = any;
}
export = THREE;
