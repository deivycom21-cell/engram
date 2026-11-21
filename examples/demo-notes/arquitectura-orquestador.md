---
title: Arquitectura del orquestador de agentes
date: 2024-02-05
type: project
---
# Arquitectura del orquestador de agentes

## Versión 1 (febrero 2024)

El orquestador se divide en microservicios: un servicio de planificación, un servicio de ejecución y un servicio de memoria. Se comunican mediante una cola de mensajes en Redis.

Decisión: usamos Redis como cola de mensajes y Postgres para guardar la memoria de los agentes, porque queríamos escalar cada servicio por separado.

La memoria de contexto para agentes de programación vive en el servicio de memoria, que guarda embeddings en Postgres con pgvector.

## Revisión del 12 de junio de 2024

Observación: el despliegue de los tres microservicios necesita 6 GB de RAM y tarda 40 minutos en arrancar en el portátil de desarrollo.

Decisión: abandonamos la arquitectura de microservicios y pasamos a un monolito modular con SQLite, porque la complejidad operativa no compensaba para un solo desarrollador y Redis consumía demasiada memoria.

La búsqueda vectorial se hará dentro de SQLite con una extensión ligera en lugar de Postgres con pgvector.
