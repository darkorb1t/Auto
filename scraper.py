import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

def scrape_api_docs(start_url, output_file="api_documentation2.txt", max_pages=30):
    visited_urls = set()
    urls_to_visit = [start_url]
    base_domain = urlparse(start_url).netloc
    page_count = 0

    print(f"\nScraping started... Output will be saved to '{output_file}'\n")

    with open(output_file, "w", encoding="utf-8") as file:
        while urls_to_visit and page_count < max_pages:
            current_url = urls_to_visit.pop(0)

            # Avoid visiting the same page twice
            if current_url in visited_urls:
                continue

            print(f"[{page_count + 1}] Scraping: {current_url}")
            try:
                response = requests.get(current_url, timeout=10)
                response.raise_for_status()
            except Exception as e:
                print(f"Error fetching {current_url}: {e}")
                continue

            visited_urls.add(current_url)
            page_count += 1
            
            soup = BeautifulSoup(response.text, 'html.parser')

            # Extract Title
            title = soup.title.string if soup.title else current_url
            file.write(f"\n\n{'='*60}\nPAGE: {title}\nURL: {current_url}\n{'='*60}\n\n")

            # Extract headers, paragraphs, and code blocks only to avoid garbage text
            for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'pre', 'code']):
                text = element.get_text(separator='\n', strip=True)
                if text:
                    file.write(text + "\n\n")

            # Find sub-pages/links for further crawling
            for link in soup.find_all('a', href=True):
                next_url = urljoin(start_url, link['href'])
                
                # Keep the crawler restricted to the exact same domain
                # and ignore anchor links (#) or media files
                if (urlparse(next_url).netloc == base_domain and 
                    next_url not in visited_urls and 
                    next_url not in urls_to_visit and 
                    '#' not in link['href']):
                    
                    if not any(ext in next_url.lower() for ext in ['.png', '.jpg', '.pdf', '.zip']):
                        urls_to_visit.append(next_url)

    print(f"\nDone! Successfully scraped {page_count} pages.")
    print(f"All text has been saved to: {output_file}")

if __name__ == "__main__":
    print("--- API Documentation Text Extractor ---")
    target_url = input("Enter the starting URL of the API Documentation: ").strip()
    
    # You can increase max_pages if the documentation is very large
    scrape_api_docs(target_url, max_pages=50)

