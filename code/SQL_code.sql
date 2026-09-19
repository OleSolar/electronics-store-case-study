use role accountadmin;
use warehouse compute_wh;
use database global_electronics_store;
use schema global_electronics_store.my_schema;

CREATE OR REPLACE TABLE sales_analysis AS

WITH products_clean AS (
    SELECT
        productkey,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(product_name), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS product_name,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(brand), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS brand,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(color), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS color,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(category), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS category,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(subcategory), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS subcategory,

        TRY_TO_DECIMAL(
            REGEXP_REPLACE(
                unit_price::VARCHAR,
                $$[^0-9.-]$$,
                ''
            ),
            12,
            2
        ) AS unit_price_usd,

        TRY_TO_DECIMAL(
            REGEXP_REPLACE(
                unit_cost::VARCHAR,
                $$[^0-9.-]$$,
                ''
            ),
            12,
            2
        ) AS unit_cost_usd

    FROM products
),

customers_clean AS (
    SELECT
        customerkey,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(gender), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS gender,

        TRY_TO_DATE(birthday) AS birthday,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(city), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS city,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(state), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS state,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(country), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS country

    FROM customers
),

stores_clean AS (
    SELECT
        storekey,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(country), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS country,

        NULLIF(
            REGEXP_REPLACE(
                TRIM(TRIM(state), '"'),
                $$[[:space:]]+$$,
                ' '
            ),
            ''
        ) AS state,

        TRY_TO_DECIMAL(
            REGEXP_REPLACE(
                square_meters::VARCHAR,
                $$[^0-9.-]$$,
                ''
            ),
            12,
            2
        ) AS square_meters,

        TRY_TO_DATE(open_date) AS open_date

    FROM stores
),

sales_clean AS (
    SELECT
        order_number,
        line_item,
        TRY_TO_DATE(order_date) AS order_date,
        TRY_TO_DATE(delivery_date) AS delivery_date,
        customerkey,
        storekey,
        productkey,
        TRY_TO_NUMBER(quantity) AS quantity,

        UPPER(
            TRIM(
                TRIM(currency_code),
                '"'
            )
        ) AS currency_code

    FROM sales
),

exchange_rates_raw AS (
    SELECT
        TRY_TO_DATE(date) AS exchange_date,

        UPPER(
            TRIM(
                TRIM(currency),
                '"'
            )
        ) AS currency_code,

        TRY_TO_DECIMAL(
            REGEXP_REPLACE(
                exchange::VARCHAR,
                $$[^0-9.-]$$,
                ''
            ),
            18,
            6
        ) AS exchange_rate

    FROM exchange_rates
),

-- Ensure there is only one exchange rate per date and currency
exchange_rates_clean AS (
    SELECT
        exchange_date,
        currency_code,
        MAX(exchange_rate) AS exchange_rate

    FROM exchange_rates_raw

    WHERE exchange_date IS NOT NULL
      AND currency_code IS NOT NULL
      AND exchange_rate IS NOT NULL
      AND exchange_rate > 0

    GROUP BY
        exchange_date,
        currency_code
),

joined_data AS (
    SELECT
        s.*,
        p.product_name,
        p.brand,
        p.color,
        p.category,
        p.subcategory,
        p.unit_price_usd,
        p.unit_cost_usd,

        c.gender,
        c.birthday,
        c.city AS customer_city,
        c.state AS customer_state,
        c.country AS customer_country,

        st.country AS store_country,
        st.state AS store_state,
        st.square_meters,
        st.open_date,

        -- Use 1 for USD if a USD exchange-rate record is absent
        COALESCE(
            er.exchange_rate,
            CASE
                WHEN s.currency_code = 'USD' THEN 1
            END
        ) AS exchange_rate

    FROM sales_clean AS s

    LEFT JOIN products_clean AS p
        ON s.productkey = p.productkey

    LEFT JOIN customers_clean AS c
        ON s.customerkey = c.customerkey

    LEFT JOIN stores_clean AS st
        ON s.storekey = st.storekey

    LEFT JOIN exchange_rates_clean AS er
        ON s.order_date = er.exchange_date
       AND s.currency_code = er.currency_code
)

SELECT
    -- Sales identifiers
    order_number,
    line_item,
    order_date,
    delivery_date,
    customerkey,
    storekey,
    productkey,
    quantity,

    -- Sales channel
    CASE
        WHEN storekey = 0 THEN 'Yes'
        ELSE 'No'
    END AS online_order,

    -- Currency
    currency_code,
    exchange_rate,

    -- Product
    product_name,
    brand,
    color,
    category,
    subcategory,

    -- USD product prices
    unit_price_usd,
    unit_cost_usd,

    -- Prices converted to the transaction currency
    ROUND(
        unit_price_usd * exchange_rate,
        2
    ) AS unit_price_local,

    ROUND(
        unit_cost_usd * exchange_rate,
        2
    ) AS unit_cost_local,

    -- Customer
    gender,
    birthday,
    customer_city,
    customer_state,
    customer_country,

    -- Store
    store_country,
    store_state,
    square_meters,
    open_date,

    -- Financial measures in USD
    ROUND(
        quantity * unit_price_usd,
        2
    ) AS revenue_usd,

    ROUND(
        quantity * unit_cost_usd,
        2
    ) AS total_cost_usd,

    ROUND(
        quantity * (unit_price_usd - unit_cost_usd),
        2
    ) AS gross_profit_usd,

    -- Financial measures in the transaction currency
    ROUND(
        quantity * unit_price_usd * exchange_rate,
        2
    ) AS revenue_local,

    ROUND(
        quantity * unit_cost_usd * exchange_rate,
        2
    ) AS total_cost_local,

    ROUND(
        quantity
        * (unit_price_usd - unit_cost_usd)
        * exchange_rate,
        2
    ) AS gross_profit_local,

    -- Margin is the same in either currency
    CASE
        WHEN unit_price_usd IS NOT NULL
             AND unit_price_usd <> 0
        THEN ROUND(
            (unit_price_usd - unit_cost_usd)
            / unit_price_usd,
            4
        )
    END AS gross_margin,

    -- Delivery duration
    CASE
        WHEN order_date IS NOT NULL
             AND delivery_date IS NOT NULL
        THEN DATEDIFF(
            'day',
            order_date,
            delivery_date
        )
    END AS delivery_days,

    -- Customer age on the order date
    CASE
        WHEN birthday IS NOT NULL
             AND order_date IS NOT NULL
        THEN
            DATEDIFF('year', birthday, order_date)
            - CASE
                WHEN DATEADD(
                    'year',
                    DATEDIFF('year', birthday, order_date),
                    birthday
                ) > order_date
                THEN 1
                ELSE 0
              END
    END AS customer_age,

    -- Store age on the order date
    CASE
        WHEN open_date IS NOT NULL
             AND order_date IS NOT NULL
        THEN
            DATEDIFF('year', open_date, order_date)
            - CASE
                WHEN DATEADD(
                    'year',
                    DATEDIFF('year', open_date, order_date),
                    open_date
                ) > order_date
                THEN 1
                ELSE 0
              END
    END AS store_age_years

FROM joined_data;

SELECT *
FROM sales_analysis
LIMIT 100;

-- Check missing values
SELECT
    COUNT(*) AS total_rows,
    COUNT_IF(unit_price_local IS NULL) AS missing_unit_prices,
    COUNT_IF(unit_cost_local IS NULL) AS missing_unit_costs,
    COUNT_IF(product_name IS NULL) AS missing_product_names
FROM sales_analysis;

-- Check currency range
SELECT
    currency_code,
    COUNT(*) AS sales_rows,
    COUNT_IF(exchange_rate IS NULL) AS missing_exchange_rates
FROM sales_analysis
GROUP BY currency_code
ORDER BY currency_code;

-- Check complete years
SELECT
    YEAR(order_date) AS order_year,
    MIN(order_date) AS first_order_date,
    MAX(order_date) AS last_order_date,
    COUNT(DISTINCT DATE_TRUNC('month', order_date)) AS months_present,
    COUNT(DISTINCT order_number) AS orders,
    ROUND(SUM(revenue_usd), 2) AS revenue_usd
FROM sales_analysis
GROUP BY YEAR(order_date)
ORDER BY order_year;

-- Check exchange rate ranges
SELECT
    currency_code,
    MIN(exchange_rate) AS minimum_rate,
    MAX(exchange_rate) AS maximum_rate,
    COUNT(*) AS sales_rows,
    COUNT_IF(exchange_rate IS NULL) AS missing_rates
FROM sales_analysis
GROUP BY currency_code
ORDER BY currency_code;



CREATE OR REPLACE VIEW annual_business_summary AS

WITH store_network AS (
    SELECT
        COUNT(DISTINCT country) AS total_store_countries,
        COUNT(DISTINCT storekey) AS total_physical_stores
    FROM stores
    WHERE storekey <> 0
),

yearly_sales AS (
    SELECT
        YEAR(order_date) AS sales_year,

        MIN(order_date) AS first_order_date,
        MAX(order_date) AS last_order_date,
        COUNT(DISTINCT DATE_TRUNC('month', order_date)) AS months_present,

        -- Geographic coverage
        COUNT(
            DISTINCT CASE
                WHEN storekey <> 0 THEN store_country
            END
        ) AS active_store_countries,

        -- Stores with sales during the year
        COUNT(
            DISTINCT CASE
                WHEN storekey <> 0 THEN storekey
            END
        ) AS active_physical_stores,

        -- Customers and orders
        COUNT(DISTINCT customerkey) AS distinct_customers,
        COUNT(DISTINCT order_number) AS distinct_orders,

        -- Financial measures
        ROUND(SUM(revenue_usd), 2) AS revenue_usd,
        ROUND(SUM(total_cost_usd), 2) AS total_cost_usd,
        ROUND(SUM(gross_profit_usd), 2) AS gross_profit_usd,

        ROUND(
            SUM(gross_profit_usd)
            / NULLIF(SUM(revenue_usd), 0),
            4
        ) AS gross_margin,

        -- Average order value
        ROUND(
            SUM(revenue_usd)
            / NULLIF(COUNT(DISTINCT order_number), 0),
            2
        ) AS average_order_value,

        -- Online performance
        ROUND(
            SUM(
                CASE
                    WHEN online_order = 'Yes'
                    THEN revenue_usd
                    ELSE 0
                END
            ),
            2
        ) AS online_revenue_usd,

        COUNT(
            DISTINCT CASE
                WHEN online_order = 'Yes'
                THEN order_number
            END
        ) AS online_orders,

        ROUND(
            SUM(
                CASE
                    WHEN online_order = 'Yes'
                    THEN revenue_usd
                    ELSE 0
                END
            )
            / NULLIF(SUM(revenue_usd), 0),
            4
        ) AS online_revenue_share

    FROM sales_analysis

    WHERE order_date IS NOT NULL

    GROUP BY YEAR(order_date)
)

SELECT
    y.sales_year,
    y.first_order_date,
    y.last_order_date,
    y.months_present,

    CASE
        WHEN y.months_present = 12 THEN 'Complete'
        ELSE 'Partial'
    END AS year_status,

    n.total_store_countries,
    n.total_physical_stores,

    y.active_store_countries,
    y.active_physical_stores,
    y.distinct_customers,
    y.distinct_orders,

    y.revenue_usd,
    y.total_cost_usd,
    y.gross_profit_usd,
    y.gross_margin,
    y.average_order_value,

    y.online_revenue_usd,
    y.online_orders,
    y.online_revenue_share

FROM yearly_sales AS y

CROSS JOIN store_network AS n;

SELECT *
FROM annual_business_summary
ORDER BY sales_year;

SELECT *
FROM sales_analysis
ORDER BY order_date, order_number, line_item;
