import time
from typing import List, Dict, Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

def _build_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

def _login_instagram(driver, user: str, password: str):
    driver.get("https://www.instagram.com/accounts/login/")
    time.sleep(2)
    driver.find_elements(By.NAME, "email")[0].send_keys(user)
    driver.find_elements(By.NAME, "pass")[0].send_keys(password)
    btn_login = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable(
            (
                By.CSS_SELECTOR,
                "#login_form > div > div.x1n2onr6.x1ja2u2z.x9f619.x78zum5.xdt5ytf.x2lah0s.x193iq5w.xz9dl7a > div > div.x9f619.x1n2onr6.x1ja2u2z.x78zum5.xdt5ytf.x2lah0s.x193iq5w.x6s0dn4.xz9dl7a.x1k70j0n.xzueoph.xzboxd6.x14l7nz5 > div > div",
            )
        )
    )
    btn_login.click()
    time.sleep(5)
    driver.refresh()

def _collect_comments(driver, limit: int) -> List[Dict]:
    data: List[Dict] = []
     # antes de scrollar, clica no botão que expande a área de comentários (se existir)
    try:
        expand_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    '/html/body/div[1]/div/div/div[2]/div/div/div[1]/div[1]/div[2]/section/main/div[1]/div[2]/div/div/div[2]/div[2]/div',
                )
            )
        )
        expand_btn.click()
        time.sleep(2)
    except Exception:
        # se o botão não existir nessa página, segue normalmente
        pass
    # scroller do painel de comentários — tenta seletores conhecidos em ordem
    scroller = None
    _scroller_candidates = [
        # layout novo de reels
        "/html/body/div[1]/div/div/div[2]/div/div/div[2]/div/div/div[1]/div[1]/div/div/div/div[1]/div/div[2]",
        # layout antigo de reels/posts
        "//div[contains(@class,'x5yr21d') and contains(@class,'xw2csxc') "
        "and contains(@class,'x1odjw0f') and contains(@class,'x1n2onr6')]",
        # fallback genérico: div scrollável dentro do artigo
        "//article//div[@role='presentation' and .//ul]",
    ]
    for xpath in _scroller_candidates:
        try:
            scroller = driver.find_element(By.XPATH, xpath)
            break
        except Exception:
            continue

    if scroller is None:
        print("[WARN] Painel de comentários não encontrado — salvando debug_instagram.html")
        with open("debug_instagram.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        return []
    # helpers
    def _get_author(c) -> Optional[str]:
        try:
            a = c.find_element(By.XPATH, ".//a[starts-with(@href,'/') and not(contains(@href,'/p/'))][1]")
            return a.text.strip() or a.get_attribute("href").split("/")[-2]
        except:
            return None

    def _get_comment(c) -> str:
      # Procura um span que não esteja dentro de um <a></a>
        try:
            return c.find_element(By.XPATH, ".//div[contains(@class,'xdt5ytf') and contains(@class,'x1cy8zhl')]//span[normalize-space()!=''][1]").text.strip()
        except:
            return ""

    def _get_datetime(c) -> Optional[str]:
        try:
            return c.find_element(By.TAG_NAME, "time").get_attribute("datetime")
        except:
            return None

    # scrolla o painel de comentários
    last_height = driver.execute_script("return arguments[0].scrollHeight", scroller)
    while len(data) < limit:
        driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", scroller)
        time.sleep(3)
        new_height = driver.execute_script("return arguments[0].scrollHeight", scroller)
        if new_height == last_height:
            break # fim da página
        last_height = new_height

        # pega os blocos de comentários
        comments = scroller.find_elements(
          By.XPATH,
          ".//a[contains(@href,'/p/') and contains(@href,'/c/')]/time"
          "/ancestor::div[.//div[contains(@class,'xdt5ytf') and contains(@class,'x1cy8zhl')]][1]"
        )

        for c in comments[len(data):limit]:
            author = _get_author(c)
            comment = _get_comment(c)
            published = _get_datetime(c)

            if not comment:
                continue

            data.append({
                "author": author,
                "comment": comment,
                "publishedAt": published
            })
            
    if not data:
        print("[WARN] Nenhum comentário coletado!")
        with open("debug_instagram.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)

    return data

def scrape_instagram_much(user: str, password: str, body: dict, limit: int = 10):
    reels = body.get("reels", []) or []
    posts = body.get("posts", []) or []
    driver = _build_driver()
    _login_instagram(driver, user, password)
    time.sleep(5)

    reels_results = []
    posts_results = []
    total_items = len(reels) + len(posts)

    try:
        for rid in reels:
            url = f"https://www.instagram.com/reel/{rid}"
            driver.get(url)
            time.sleep(5)
            print(f"🎬 Varrendo reel {rid}")
            data = _collect_comments(driver, limit)
            reels_results.append({"id": rid, "data": data})

        for pid in posts:
            url = f"https://www.instagram.com/p/{pid}"
            driver.get(url)
            time.sleep(5)
            print(f"📸 Varrendo post {pid}")
            data = _collect_comments(driver, limit)
            posts_results.append({"id": pid, "data": data})

    finally:
        driver.quit()
        print(f"🧹 Sessão encerrada após {total_items} itens.")

    return {"summary": {"total": total_items, "success": len(reels_results + posts_results)}, "reels": reels_results, "posts": posts_results}

def scrape_instagram_one(user: str, password: str, mode: str, id: str, limit: int = 10) -> List[Dict]:
    try:
        driver = _build_driver()
        _login_instagram(driver, user, password)
        if mode == "reel":
            url = f"https://www.instagram.com/reel/{id}"
        else:
            url = f"https://www.instagram.com/p/{id}"
        driver.get(url)
        return _collect_comments(driver, limit)
    finally:
        driver.quit()