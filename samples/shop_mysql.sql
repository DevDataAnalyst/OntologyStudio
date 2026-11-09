CREATE TABLE customers (
  customer_id INT AUTO_INCREMENT PRIMARY KEY,
  first_name VARCHAR(50) NOT NULL,
  email VARCHAR(120) NOT NULL UNIQUE,
  tier ENUM('gold','silver') DEFAULT 'silver',
  referred_by INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (referred_by) REFERENCES customers(customer_id)
) ENGINE=InnoDB COMMENT='People who buy from the shop';

CREATE TABLE products (
  product_id INT PRIMARY KEY,
  sku VARCHAR(30) NOT NULL,
  name VARCHAR(200) NOT NULL,
  price DECIMAL(10,2) NOT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  UNIQUE KEY uq_sku (sku)
) ENGINE=InnoDB;

CREATE TABLE categories (category_id INT PRIMARY KEY, title VARCHAR(80) NOT NULL);

CREATE TABLE product_categories (
  product_id INT NOT NULL,
  category_id INT NOT NULL,
  PRIMARY KEY (product_id, category_id),
  CONSTRAINT fk_pc_p FOREIGN KEY (product_id) REFERENCES products(product_id),
  CONSTRAINT fk_pc_c FOREIGN KEY (category_id) REFERENCES categories(category_id)
);

CREATE TABLE orders (
  order_id BIGINT PRIMARY KEY,
  customer_id INT NOT NULL,
  order_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);
ALTER TABLE orders ADD CONSTRAINT fk_o_c FOREIGN KEY (customer_id) REFERENCES customers(customer_id);

CREATE TABLE order_items (
  order_id BIGINT NOT NULL,
  line_no INT NOT NULL,
  product_id INT NOT NULL,
  quantity INT NOT NULL,
  PRIMARY KEY (order_id, line_no),
  FOREIGN KEY (order_id) REFERENCES orders(order_id),
  FOREIGN KEY (product_id) REFERENCES products(product_id)
);
