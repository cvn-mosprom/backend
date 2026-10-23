// The vendored lib references `three` only in type positions (minigames/types.ts; the rendering code that really uses
// it is never imported by the server). Stub the few names so the typecheck does not need the real package.
declare namespace THREE {
	type Object3D = any;
	type Group = any;
	type Camera = any;
}
export = THREE;
