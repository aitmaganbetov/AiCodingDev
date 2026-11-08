package main

import rego.v1

deny contains msg if {
	input.kind == "Deployment"
	some c in input.spec.template.spec.containers
	c.securityContext.privileged == true
	msg := sprintf("container %s must not be privileged", [c.name])
}

warn contains msg if {
	input.kind == "Deployment"
	not input.spec.template.spec.securityContext.runAsNonRoot
	msg := "pod should set runAsNonRoot"
}
