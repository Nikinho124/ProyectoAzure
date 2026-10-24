# Arquitectura y Reglas - FarmaSistem

## 1. Stack Tecnologico
* Backend: Python 3.11, Django 4.2 LTS, DRF.
* BD: PostgreSQL 15.3.
* Frontend: React.js 18, Tailwind.
* Seguridad: JWT, python-dotenv.

## 2. Reglas por Capas
* Frontend: Bloquear inputs, obligar RUT en controlados, modulo para subir fotos de recetas y pasarela simulada de pagos.
* Backend: Validar RUT, logica FEFO, procesamiento de imagenes (recetas), control estricto de stock critico.
* Datos: Borrado logico, constraints y triggers.

## 3. Roles y Permisos (RBAC)
* Operador_POS: Venta normal y lectura de catalogo.
* Auditor_QF (Quimico Farmaceutico): Control y venta de recetas retenidas. Sube imagen de la receta, registra datos del medico prescriptor y justifica mermas (Firma Hash).
* Jefe de Farmacia (NUEVO): Privilegios de excepcion. Define condiciones (ej. tope maximo de medicamentos por cliente), autoriza ventas bloqueadas (ej. recetas poco legibles) y supervisa ingresos criticos.
* Administrador: Gestiona usuarios.

## 4. Auditoria
* Middleware de logs inmutables (ID, IP, accion).

## 5. Pruebas y Flujos de Negocio
* Base de datos inicial: Debe estar poblada con 20 a 30 medicamentos de prueba.
* Ingreso Critico: Alertas de control alto al ingresar volumen de stock critico (ej. 10 cajas).
* Flujo Receta Retenida: Registro de medico -> Subida de foto de receta -> Simulacion de input de pago.