# External baseline: the intent's predicates evaluated by a policy engine
# (Open Policy Agent) against context that XAIR replicates into its data store
# after every update, as one document with the global version, the per-path
# versions, and the trust flag of the snapshot.
# Input: {"predicates": [{"path": [...], "op": "==", "value": ...}, ...],
#         "expected_version": <read-set version observed at validation>}
# allow: predicates only (no version); allow_versioned: also requires the
# read-set version to equal the expected one, the same condition XAIR's gate uses.
package xair

import rego.v1

state := data.xairstate

default allow := false

allow if {
	state.trusted == true
	count(violations) == 0
}

violations contains p if {
	some p in input.predicates
	not holds(p)
}

holds(p) if {
	v := object.get(state.context, p.path, null)
	v != null
	cmp(p.op, v, p.value)
}

read_paths contains concat(".", p.path) if some p in input.predicates

related(a, b) if a == b

related(a, b) if startswith(a, concat("", [b, "."]))

related(a, b) if startswith(b, concat("", [a, "."]))

readset_version := max(array.concat([0], [v |
	some k, v in state.path_versions
	some r in read_paths
	related(k, r)
]))

default allow_versioned := false

allow_versioned if {
	allow
	readset_version == input.expected_version
}

decision := {
	"allow": allow,
	"allow_versioned": allow_versioned,
	"version": object.get(state, "version", -1),
	"readset_version": readset_version,
}

cmp("==", a, b) if a == b

cmp("!=", a, b) if a != b

cmp("<", a, b) if {
	is_number(a)
	is_number(b)
	a < b
}

cmp("<=", a, b) if {
	is_number(a)
	is_number(b)
	a <= b
}

cmp(">", a, b) if {
	is_number(a)
	is_number(b)
	a > b
}

cmp(">=", a, b) if {
	is_number(a)
	is_number(b)
	a >= b
}
