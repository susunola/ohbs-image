package ohbs_image.admission

import rego.v1

default allow := false

allow if {
    input.schema == "https://ohbs-image.dev/consumer-admission/v1"
    input.allowed == true
    input.artifact.status == "active"
}

deny contains message if {
    some check in input.policy_decision.checks
    check.result == "deny"
    message := sprintf("ohbs-image control %q denied the artifact", [check.control])
}

deny contains "artifact is not active" if {
    input.artifact.status != "active"
}
