"""Identidades controladas del laboratorio local, nunca tomadas de HTTP o del modelo."""
from dataclasses import replace
from .composition import demo_identity


def fixture_identity(kind):
    if kind not in ('operator','approver'):raise ValueError('Identidad de desarrollo desconocida')
    original=demo_identity()
    return (replace(original,roles=original.roles|{'operator'}) if kind=='operator' else
        replace(original,actor_id='approver-demo',roles=frozenset({'approver'})))
