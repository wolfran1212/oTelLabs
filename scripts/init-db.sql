-- ════════════════════════════════════════════════════════════════════
-- init-db.sql — Esquema inicial para el laboratorio OTel
-- Se ejecuta automáticamente al iniciar el contenedor de PostgreSQL
-- ════════════════════════════════════════════════════════════════════

-- Tabla de pedidos (consumida por service-a)
CREATE TABLE IF NOT EXISTS orders (
    id          VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    product     VARCHAR(100) NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
    customer_id VARCHAR(50),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product);

-- Tabla de inventario (consumida por service-b)
CREATE TABLE IF NOT EXISTS inventory (
    product_id    VARCHAR(100) PRIMARY KEY,
    available     INTEGER NOT NULL DEFAULT 0,
    warehouse     VARCHAR(50) NOT NULL DEFAULT 'WH-MAIN',
    last_updated  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    reserved      INTEGER NOT NULL DEFAULT 0
);

-- Datos de prueba: pedidos
INSERT INTO orders (id, product, quantity, status, customer_id) VALUES
    ('ord-001', 'LAPTOP-X1',   1, 'delivered', 'cust-100'),
    ('ord-002', 'MOUSE-PRO',   2, 'pending',   'cust-101'),
    ('ord-003', 'KEYBOARD-MK', 1, 'shipped',   'cust-102'),
    ('ord-004', 'MONITOR-4K',  1, 'pending',   'cust-103'),
    ('ord-005', 'HEADSET-Z',   1, 'cancelled', 'cust-104')
ON CONFLICT (id) DO NOTHING;

-- Datos de prueba: inventario
INSERT INTO inventory (product_id, available, warehouse, reserved) VALUES
    ('LAPTOP-X1',   15, 'WH-BOGOTA', 2),
    ('MOUSE-PRO',   50, 'WH-MAIN',   5),
    ('KEYBOARD-MK', 30, 'WH-MAIN',   0),
    ('MONITOR-4K',   8, 'WH-BOGOTA', 1),
    ('HEADSET-Z',   22, 'WH-MAIN',   0)
ON CONFLICT (product_id) DO NOTHING;

-- Función para actualizar updated_at automáticamente
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
