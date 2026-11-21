---
title: Ideas sobre automatización
date: 2024-01-10
tags: [automatizacion, agentes]
---
# Ideas sobre automatización

Paso demasiado tiempo repitiendo el mismo contexto cada vez que abro una sesión con un agente de programación. Cada conversación empieza desde cero y el agente no sabe qué decidimos la semana pasada.

Idea: construir una herramienta para agentes de programación que recuerde el contexto del proyecto entre sesiones: decisiones, convenciones y errores ya resueltos.

La herramienta podría exponer la memoria como un servidor local, de modo que cualquier agente consulte "qué sabemos de este módulo" antes de escribir código.

## Automatizar tareas repetitivas

Idea: automatizar la revisión de dependencias con un script nocturno que abra un informe con las librerías desactualizadas.

¿Cómo evitamos que la memoria del agente se llene de información obsoleta?

Ver [[Arquitectura del orquestador de agentes]] para el diseño técnico.
